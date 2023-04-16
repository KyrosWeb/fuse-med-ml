import copy
import logging
import os
import pathlib
import shutil
import time
from collections import OrderedDict

import pandas as pd
import pytorch_lightning as pl
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import yaml
from torch.utils.data.dataloader import DataLoader

import fuse.utils.gpu as GPU
from fuse.data.utils.collates import CollateDefault
from fuse.data.utils.samplers import BatchSamplerDefault
from fuse.dl.lightning.pl_module import LightningModuleDefault
from fuse.dl.losses.loss_default import LossDefault
from fuse.dl.models import ModelMultiHead
from fuse.dl.models.backbones.backbone_resnet_3d import BackboneResnet3D
from fuse.dl.models.heads.heads_3D import Head3D
from fuse.eval.metrics.classification.metrics_classification_common import MetricAccuracy, MetricAUCROC, MetricConfusion
from fuse.eval.metrics.classification.metrics_thresholding_common import MetricApplyThresholds
from fuse.utils.rand.seed import Seed
from fuse.utils.utils_logger import fuse_logger_start
from fuseimg.datasets.knight import KNIGHT

## Parameters:
##############################################################################
# Data sources to use in model. Set {'imaging': True, 'clinical': False} for imaging only setting,
# and vice versa, or set both to True to use both.
# allocate gpus
# uncomment if you want to use specific gpus instead of automatically looking for free ones


def make_model(use_data: dict, num_classes: int, imaging_dropout: float, fused_dropout: float):
    if use_data["imaging"]:
        backbone = BackboneResnet3D(in_channels=1, pretrained=False)
        conv_inputs = [("model.backbone_features", 512)]
    else:
        backbone = nn.Identity()
        conv_inputs = None
    if use_data["clinical"]:
        append_features = [("data.input.clinical.all", 11)]
    else:
        append_features = None

    model = ModelMultiHead(
        conv_inputs=(("data.input.img", 1),),
        backbone=backbone,
        heads=[
            Head3D(
                head_name="head_0",
                mode="classification",
                conv_inputs=conv_inputs,
                dropout_rate=imaging_dropout,
                num_outputs=num_classes,
                append_features=append_features,
                append_layers_description=(256, 128),
                fused_dropout_rate=fused_dropout,
            ),
        ],
    )
    return model


def main(cfg_path):
    # read config params
    cfg = yaml.safe_load(open(cfg_path))
    task_num = cfg["task_num"]
    num_classes = cfg[task_num]["num_classes"]
    target_name = cfg[task_num]["target_name"]
    class_names = None if not cfg[task_num]["class_names"] else cfg[task_num]["class_names"]
    if cfg["num_gpus"] == 0:
        use_gpu = False
    else:
        use_gpu = True

    # read train/val splits file. for convenience, we use the one
    # auto-generated by the nnU-Net framework for the KiTS21 data
    dir_path = pathlib.Path(__file__).parent.resolve()
    splits = pd.read_pickle(os.path.join(dir_path, "splits_final.pkl"))
    # For this example, we use split 0 out of the 5 available cross validation splits
    split = splits[0]
    if cfg["testing"]:
        split["train"] = split["train"][:10]
        split["val"] = split["val"][:10]
    # read environment variables for data, cache and results locations
    data_path = os.environ["KNIGHT_DATA"]
    cache_path = os.path.join(os.environ["KNIGHT_CACHE"], str(cfg["experiment_num"]))
    results_path = os.environ["KNIGHT_RESULTS"]

    ## Basic settings:
    ##############################################################################
    # create model results dir:
    # we use a time stamp in model directory name, to prevent re-writing
    timestr = time.strftime("%Y%m%d-%H%M%S")
    model_dir = os.path.join(results_path, timestr)
    if not os.path.isdir(model_dir):
        os.makedirs(model_dir)
    shutil.copy(cfg_path, model_dir)
    # start logger
    fuse_logger_start(output_path=model_dir, console_verbose_level=logging.INFO)
    print("Done")

    # set constant seed for reproducibility.
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"  # required for pytorch deterministic mode
    rand_gen = Seed.set_seed(1234, deterministic_mode=True)

    # select gpus
    if use_gpu:
        GPU.choose_and_enable_multiple_gpus(cfg["num_gpus"], force_gpus=None)

    ## Model definition
    ##############################################################################

    model = make_model(cfg["use_data"], num_classes, cfg["imaging_dropout"], cfg["fused_dropout"])

    ## FuseMedML dataset preparation
    ##############################################################################

    train_ds, valid_ds = KNIGHT.dataset(
        data_path=data_path,
        cache_dir=cache_path,
        split=split,
        reset_cache=cfg["reset_cache"],
        resize_to=cfg["resize_to"],
    )

    ## Create dataloader

    sampler = BatchSamplerDefault(
        dataset=train_ds,
        balanced_class_name=target_name,
        num_balanced_classes=num_classes,
        batch_size=cfg["batch_size"],
        balanced_class_weights=[1.0 / num_classes] * num_classes if task_num == "task_2" else None,
        mode="approx",
    )

    train_dl = DataLoader(
        dataset=train_ds,
        shuffle=False,
        drop_last=False,
        batch_sampler=sampler,
        collate_fn=CollateDefault(),
        num_workers=8,
    )

    valid_dl = DataLoader(
        dataset=valid_ds,
        shuffle=False,
        drop_last=False,
        batch_sampler=None,
        batch_size=cfg["batch_size"],
        num_workers=8,
        collate_fn=CollateDefault(),
        generator=rand_gen,
    )

    # Loss definition:
    ##############################################################################
    losses = {
        "cls_loss": LossDefault(pred="model.logits.head_0", target=target_name, callable=F.cross_entropy, weight=1.0),
    }

    # Metrics definition:
    ##############################################################################
    train_metrics = OrderedDict(
        [
            ("op", MetricApplyThresholds(pred="model.output.head_0")),  # will apply argmax
            ("auc", MetricAUCROC(pred="model.output.head_0", target=target_name, class_names=class_names)),
            ("accuracy", MetricAccuracy(pred="results:metrics.op.cls_pred", target=target_name)),
            (
                "sensitivity",
                MetricConfusion(pred="results:metrics.op.cls_pred", target=target_name, metrics=("sensitivity",)),
            ),
        ]
    )
    val_metrics = copy.deepcopy(train_metrics)  # use the same metrics in validation as well

    best_epoch_source = dict(
        monitor=cfg[task_num]["target_metric"],  # can be any key from losses or metrics dictionaries
        mode="max",  # can be either min/max
    )

    # Optimizer definition:
    ##############################################################################
    optimizer = optim.Adam(model.parameters(), lr=cfg["learning_rate"], weight_decay=0.001)

    # Scheduler definition:
    ##############################################################################
    lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer)
    lr_sch_config = dict(scheduler=lr_scheduler, monitor="validation.losses.total_loss")
    optimizers_and_lr_schs = dict(optimizer=optimizer, lr_scheduler=lr_sch_config)

    ## Training
    ##############################################################################

    # create instance of PL module - FuseMedML generic version
    pl_module = LightningModuleDefault(
        model_dir=model_dir,
        model=model,
        losses=losses,
        train_metrics=train_metrics,
        validation_metrics=val_metrics,
        best_epoch_source=best_epoch_source,
        optimizers_and_lr_schs=optimizers_and_lr_schs,
    )
    # create lightining trainer.
    pl_trainer = pl.Trainer(
        default_root_dir=model_dir,
        max_epochs=cfg["num_epochs"],
        accelerator="gpu" if use_gpu else "cpu",
        devices=cfg["num_gpus"] if use_gpu else None,
        strategy=None,
        auto_select_gpus=True if use_gpu else False,
        num_sanity_val_steps=-1,
        auto_scale_batch_size="binsearch",
    )

    # train
    pl_trainer.fit(pl_module, train_dl, valid_dl, ckpt_path=None)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    main(config_path)

from fuse.data.datasets.dataset_default import DatasetDefault
from fuse.data.datasets.caching.samples_cacher import SamplesCacher
from fuseimg.data.ops.image_loader import OpLoadImage
from fuseimg.data.ops.color import OpNormalizeAgainstSelf
from fuseimg.data.ops.aug.geometry import OpAugAffine2D, OpAugSqueeze3Dto2D, OpAugUnsqueeze3DFrom2D
from fuse.data import PipelineDefault, OpToTensor, OpRepeat
from fuse.data.ops.ops_common import OpLambda
from fuseimg.data.ops.aug.color import OpAugColor
from fuseimg.data.ops.aug.geometry import OpAugAffine2D
from fuse.data.ops.ops_common import OpConcat, OpLambda, OpLookup, OpToOneHot
from fuse.data.ops.ops_aug_common import OpSample, OpRandApply, OpSampleAndRepeat
from fuse.data.ops.ops_read import OpReadDataframe
from fuse.data.ops.ops_cast import OpToNumpy
from fuse.data.ops.op_base import OpBase
from fuse.utils import NDict
from functools import partial
import nibabel as nib
from typing import Hashable, Optional, Sequence
import torch
import pandas as pd
import numpy as np
import skimage
import pydicom
import os
import glob
from pathlib import Path
from fuse.data.utils.sample import get_sample_id
from medpy.io import load
from fuse.utils.rand.param_sampler import Uniform, RandInt, RandBool



class OpPICAISampleIDDecode(OpBase):
    """
    decodes sample id into image and segmentation filename
    """

    def __call__(self, sample_dict: NDict) -> NDict:
        """ """
        sid = get_sample_id(sample_dict)

        img_filename_key = "data.input.img_path"
        sample_dict[img_filename_key] = sid

        return sample_dict

class OpLoadPICAIImage(OpBase):
    """
    Loads a medical image
    """

    def __init__(self, dir_path: str, seuqences: Sequence[str] = ["_t2w"], **kwargs):
        super().__init__(**kwargs)
        self._dir_path = dir_path
        self._sequences = seuqences

    def __call__(
        self,
        sample_dict: NDict,
        key_in: str,
        key_out: str,
    ):
        """
        :param key_in: the key name in sample_dict that holds the filename
        :param key_out: the key name in sample_dict that holds the image
        :param key_metadata_out : the key to hold metadata dictionary
        """
        for seq in self._sequences:
            img_filename = os.path.join(self._dir_path,sample_dict[key_in].split("_")[0],sample_dict[key_in]+seq+".mha")

            image_data, image_header = load(img_filename)
            sample_dict[key_out+seq] = image_data
        return sample_dict

class OpLoadPICAISegmentation(OpBase):
    """
    Loads a medical image
    """

    def __init__(self, data_dir:str , dir_path: str, seuqences: Sequence[str] = ["_t2w"], **kwargs):
        super().__init__(**kwargs)
        self._dir_path = dir_path
        self._data_dir = data_dir
        # self._sequences = seuqences

    def __call__(
        self,
        sample_dict: NDict,
        key_in: str,
        key_out: str,
    ):
        """
        :param key_in: the key name in sample_dict that holds the filename
        :param key_out: the key name in sample_dict that holds the image
        :param key_metadata_out : the key to hold metadata dictionary
        """
        # for seq in self._sequences:
        img_filename = os.path.join(self._dir_path, sample_dict[key_in]+".nii.gz")
        if os.path.exists(img_filename):
            my_img  = nib.load(img_filename)
            nii_data = my_img.get_fdata()
            sample_dict[key_out] = nii_data
        else:
            img_filename = os.path.join(self._data_dir,sample_dict[key_in].split("_")[0],sample_dict[key_in]+"_t2w.mha")
            image_data, image_header = load(img_filename)
            nii_data = np.zeros(image_data.shape)
        sample_dict[key_out] = nii_data
        return sample_dict

class PICAI:
    """
    """
    @staticmethod
    def static_pipeline(data_dir: str,seg_dir:str, target: str, repeat_images :Sequence[NDict]) -> PipelineDefault:
        """
        Get suggested static pipeline (which will be cached), typically loading the data plus design choices that we won't experiment with.
        :param data_path: path to original kits21 data (can be downloaded by KITS21.download())
        """
        static_pipeline = PipelineDefault(
            "cmmd_static",
            [
                # decoding sample ID
                (OpPICAISampleIDDecode(), dict()),  # will save image and seg path to "data.input.img_path"
                (OpLoadPICAIImage(data_dir), dict(key_in="data.input.img_path", key_out="data.input.img")),
                (OpLoadPICAISegmentation(data_dir,seg_dir), dict(key_in="data.input.img_path", key_out="data.gt.seg")),
                (OpRepeat((OpLambda(partial(skimage.transform.resize,
                                                output_shape=(23, 320, 320),
                                                mode='reflect',
                                                anti_aliasing=True,
                                                preserve_range=True))),kwargs_per_step_to_add = repeat_images),{}) ,
                (OpRepeat((OpNormalizeAgainstSelf()),kwargs_per_step_to_add = repeat_images),{}) ,
                (OpRepeat((OpToNumpy()),kwargs_per_step_to_add = repeat_images),{}) ,
                
                # (OpResizeAndPad2D(), dict(key="data.input.img", resize_to=(2200, 1200), padding=(60, 60))),
            ],
        )
        return static_pipeline

    @staticmethod
    def dynamic_pipeline(data_source: pd.DataFrame,train: bool = False,repeat_images =Sequence[NDict], aug_params: NDict = None ):
        """
        Get suggested dynamic pipeline. including pre-processing that might be modified and augmentation operations.
        :param train : True iff we request dataset for train purpouse
        """
        ops = []
        bool_map = {"NO": 0, "YES": 1}
        ops +=[
                (OpRepeat((OpToTensor()),kwargs_per_step_to_add = repeat_images),{}) ,
                (OpRepeat((OpLambda(partial(torch.unsqueeze, dim=0))),kwargs_per_step_to_add = repeat_images),{}) ,
                (
                    OpReadDataframe(
                        data_source,
                        key_column="index",
                        key_name="data.input.img_path",
                        #'psa','psad','prostate_volume','histopath_type','lesion_GS','lesion_ISUP','case_ISUP'
                        columns_to_extract=['index','patient_id','study_id','mri_date','patient_age','case_csPCa'],
                        rename_columns=dict(
                            patient_id="data.patientID", case_csPCa="data.gt.classification"
                        ),
                    ),
                    dict(),
                ),
                (OpLookup(bool_map), dict(key_in="data.gt.classification", key_out="data.gt.classification")),
            ]
        if train:
            ops +=[
                    # affine augmentation - will apply the same affine transformation on each slice
                    
                    (OpRepeat((OpAugSqueeze3Dto2D()),kwargs_per_step_to_add = repeat_images), dict(axis_squeeze=1)) ,
                    # (OpRandApply(OpSampleAndRepeat(OpAugAffine2D(),kwargs_per_step_to_add = repeat_images), aug_params['apply_aug_prob']),
                    #      dict(
                    #           rotate=Uniform(*aug_params['rotate']),
                    #           scale=Uniform(*aug_params['scale']),
                    #           flip=(aug_params['flip'], aug_params['flip']),
                    #           translate=(RandInt(*aug_params['translate']), RandInt(*aug_params['translate'])))),
                    
                    (OpRepeat(OpAugUnsqueeze3DFrom2D(),kwargs_per_step_to_add = repeat_images), dict( axis_squeeze=1, channels=1)),
                ]
        dynamic_pipeline = PipelineDefault("picai_dynamic", ops)
        return dynamic_pipeline


    @staticmethod
    def dataset(
        paths: NDict,
        train_cfg: NDict,
        reset_cache: bool = True,
        sample_ids: Optional[Sequence[Hashable]] = None,
        train: bool = False,
    ):
        """
        Creates Fuse Dataset single object (either for training, validation and test or user defined set)
        :param data_dir:                    dataset root path
        :param clinical_file                path to clinical_file
        :param target                       target name used from the ground truth dataframe
        :param cache_dir:                   Optional, name of the cache folder
        :param reset_cache:                 Optional,specifies if we want to clear the cache first
        :param sample_ids: dataset including the specified sample_ids or None for all the samples. sample_id is case_{id:05d} (for example case_00001 or case_00100).
        :param train: True if used for training  - adds augmentation operations to the pipeline
        :return: DatasetDefault object
        """

        input_source_gt = pd.read_csv(paths["clinical_file"])
        input_source_gt['index'] = input_source_gt['patient_id'].astype(str)+"_"+input_source_gt['study_id'].astype(str)
        all_sample_ids = input_source_gt['index'].to_list()

        if sample_ids is None:
            sample_ids = all_sample_ids

        sequences = ["_t2w"]
        repeat_images = [dict(key="data.input.img"+seq) for seq in sequences]
        repeat_images.append(dict(key="data.gt.seg"))
        static_pipeline = PICAI.static_pipeline(paths["data_dir"],paths["seg_dir"], train_cfg["target"],repeat_images)
        dynamic_pipeline = PICAI.dynamic_pipeline(input_source_gt,train=train,repeat_images=repeat_images,aug_params=train_cfg["aug_params"])

        cacher = SamplesCacher(
            "cache_ver",
            static_pipeline,
            cache_dirs=[paths["cache_dir"]],
            restart_cache=reset_cache,
            audit_first_sample=False,
            audit_rate=None,
            workers=train_cfg["num_workers"],
        )

        my_dataset = DatasetDefault(
            sample_ids=sample_ids,
            static_pipeline=static_pipeline,
            dynamic_pipeline=dynamic_pipeline,
            cacher=cacher,
        )

        my_dataset.create()
        return my_dataset

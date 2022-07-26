import os
from fuse_examples.fuse_examples_utils import get_fuse_examples_user_dir


def get_duke_user_dir() -> str:
    return os.path.join(get_fuse_examples_user_dir(), "_duke")


def get_duke_radiomics_user_dir() -> str:
    return os.path.join(get_fuse_examples_user_dir(), "_duke_radiomics")

"""Explicit approval for synthetic fixtures; production defaults to no upload."""
from vision import process_images as _process_images
from archive import sync_range as _sync_range


def process_images(*args,**kwargs):
    kwargs.setdefault("confirm",lambda plan:True)
    return _process_images(*args,**kwargs)


def sync_range(*args,**kwargs):
    kwargs.setdefault("confirm_vlm",lambda plan:True)
    return _sync_range(*args,**kwargs)

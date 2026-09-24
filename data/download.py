#!/usr/bin/env python3
"""Download the gloss-example-data dataset into this folder.

Fetches https://huggingface.co/datasets/chenyuec/gloss-example-data and
places its contents next to ``config.yaml``, so the relative folders in that
config resolve without editing:

    mesh/<name>/          scene.gltf, scene.bin, textures/, license.txt
    single_view/<name>/   viewNNNN.basecolor.png   reference views
    texture/<name>/       viewNNNN.png             partial UV texture per view
    metas/<name>/         viewNNNN.yml             reference cameras and prompts
    brush/                brush presets

The dataset is public; no Hugging Face login is needed.
"""

import argparse
import os
import sys

REPO_ID = "chenyuec/gloss-example-data"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(
        description=f"Download {REPO_ID} into {DATA_DIR}",
        epilog="Example: python data/download.py mesh single_view",
    )
    parser.add_argument(
        "folders",
        nargs="*",
        help="top-level dataset folders to fetch (default: everything)",
    )
    args = parser.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("huggingface_hub is not installed. Run: pip install huggingface_hub")

    patterns = [f"{name.strip('/')}/**" for name in args.folders] or None
    try:
        path = snapshot_download(
            repo_id=REPO_ID,
            repo_type="dataset",
            local_dir=DATA_DIR,
            allow_patterns=patterns,
        )
    except Exception as exc:  # noqa: BLE001 - surface auth and network errors plainly
        sys.exit(
            f"Download failed: {exc}\n"
            "Check the network connection and retry."
        )
    print(f"Downloaded {REPO_ID} to {path}")
    print("Load data/config.yaml from the Gloss panel to use it.")


if __name__ == "__main__":
    main()

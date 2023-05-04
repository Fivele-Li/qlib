#  Copyright (c) Microsoft Corporation.
#  Licensed under the MIT License.
import logging
import sys
import os
from pathlib import Path

import qlib
import fire
import ruamel.yaml as yaml
from qlib.config import C
from qlib.model.trainer import task_train
from qlib.utils.data import update_config
from qlib.log import get_module_logger

logger = get_module_logger("qrun", logging.INFO)


def get_path_list(path):
    if isinstance(path, str):
        return [path]
    else:
        return list(path)


def sys_config(config, config_path):
    """
    Configure the `sys` section

    Parameters
    ----------
    config : dict
        configuration of the workflow.
    config_path : str
        path of the configuration
    """
    sys_config = config.get("sys", {})

    # abspath
    for p in get_path_list(sys_config.get("path", [])):
        sys.path.append(p)

    # relative path to config path
    for p in get_path_list(sys_config.get("rel_path", [])):
        sys.path.append(str(Path(config_path).parent.resolve().absolute() / p))


# workflow handler function
def workflow(config_path, experiment_name="workflow", uri_folder="mlruns"):
    """
    This is a Qlib CLI entrance.
    User can run the whole Quant research workflow defined by a configure file
    - the code is located here ``qlib/workflow/cli.py`

    User can specify a base_config file in your workflow.yml file by adding "base_config_path".
    Qlib will load the configuration in base_config_path first, and the user only needs to update the custom fields
    in their own workflow.yml file.

    For examples:

        qlib_init:
            provider_uri: "~/.qlib/qlib_data/cn_data"
            region: cn
        BASE_CONFIG_PATH: "workflow_config_lightgbm_Alpha158_csi500.yaml"
        market: csi300

    """
    with open(config_path) as fp:
        config = yaml.safe_load(fp)

    base_config_path = config.get("BASE_CONFIG_PATH", None)
    if base_config_path:
        logger.info(f"Use BASE_CONFIG: {base_config_path}")
        base_config_path = Path(base_config_path)

        # it will find config file in absolute path and relative path
        if not base_config_path.exists():
            raise FileNotFoundError(f"Can't find the BASE_CONFIG file: {base_config_path}")

        with open(base_config_path) as fp:
            base_config = yaml.safe_load(fp)
    else:
        base_config = {}

    config = update_config(base_config, config)

    # config the `sys` section
    sys_config(config, config_path)

    if "exp_manager" in config.get("qlib_init"):
        qlib.init(**config.get("qlib_init"))
    else:
        exp_manager = C["exp_manager"]
        exp_manager["kwargs"]["uri"] = "file:" + str(Path(os.getcwd()).resolve() / uri_folder)
        qlib.init(**config.get("qlib_init"), exp_manager=exp_manager)

    if "experiment_name" in config:
        experiment_name = config["experiment_name"]
    recorder = task_train(config.get("task"), experiment_name=experiment_name)
    recorder.save_objects(config=config)


# function to run workflow by config
def run():
    fire.Fire(workflow)


if __name__ == "__main__":
    run()

# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import pickle

__dir__ = os.path.dirname(os.path.abspath(__file__))
sys.path.append(__dir__)
sys.path.append(os.path.abspath(os.path.join(__dir__, "..")))

import paddle
from ppocr.data import build_dataloader, set_signal_handlers
from ppocr.modeling.architectures import build_model
from ppocr.postprocess import build_post_process
from ppocr.utils.save_load import load_model
import tools.program as program


def main():
    global_config = config["Global"]
    # build dataloader
    config["Eval"]["dataset"]["name"] = config["Train"]["dataset"]["name"]
    config["Eval"]["dataset"]["data_dir"] = config["Train"]["dataset"]["data_dir"]
    config["Eval"]["dataset"]["label_file_list"] = config["Train"]["dataset"][
        "label_file_list"
    ]
    set_signal_handlers()
    eval_dataloader = build_dataloader(config, "Eval", device, logger)

    # build post process
    post_process_class = build_post_process(config["PostProcess"], global_config)

    # build model
    # for rec algorithm
    if hasattr(post_process_class, "character"):
        char_num = len(getattr(post_process_class, "character"))
        config["Architecture"]["Head"]["out_channels"] = char_num

    # set return_features = True
    config["Architecture"]["Head"]["return_feats"] = True

    model = build_model(config["Architecture"])

    # amp
    use_amp = config["Global"].get("use_amp", False)
    amp_level = config["Global"].get("amp_level", "O2")
    amp_custom_black_list = config["Global"].get("amp_custom_black_list", [])
    if use_amp:
        AMP_RELATED_FLAGS_SETTING = {
            "FLAGS_cudnn_batchnorm_spatial_persistent": 1,
        }
        paddle.set_flags(AMP_RELATED_FLAGS_SETTING)
        scale_loss = config["Global"].get("scale_loss", 1.0)
        use_dynamic_loss_scaling = config["Global"].get(
            "use_dynamic_loss_scaling", False
        )
        scaler = paddle.amp.GradScaler(
            init_loss_scaling=scale_loss,
            use_dynamic_loss_scaling=use_dynamic_loss_scaling,
        )
        if amp_level == "O2":
            model = paddle.amp.decorate(
                models=model, level=amp_level, master_weight=True
            )
    else:
        scaler = None

    best_model_dict = load_model(
        config, model, model_type=config["Architecture"]["model_type"]
    )
    if len(best_model_dict):
        logger.info("metric in ckpt ***************")
        for k, v in best_model_dict.items():
            logger.info("{}:{}".format(k, v))

    # get features from train data
    char_center = program.get_center(
        model,
        eval_dataloader,
        post_process_class,
        scaler,
        amp_level,
        amp_custom_black_list,
    )
    logger.info("Number of character centers: {}".format(len(char_center)))

    # serialize to disk
    with open("./train_center.pkl", "wb") as f:
        pickle.dump(char_center, f)
    return


if __name__ == "__main__":
    config, device, logger, vdl_writer = program.preprocess()
    main()

# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserve.
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

import math
import paddle
import paddle.nn as nn
import paddle.nn.functional as F
import paddle.distributed as dist

from ..backbones import build_backbone
from .builder import MODELS


@MODELS.register()
class LVViTWrapper(nn.Layer):
    def __init__(
            self,
            architecture=None, ):
        """A wrapper for a LVViT model as specified in the paper.

        Args:
            architecture (dict): A dictionary containing the LVViT instantiation parameters.
        """
        super().__init__()

        self.backbone = build_backbone(architecture)
        self.automatic_optimization = False

        self.loss = LVViTLoss()

    def train_iter(self, *inputs, **kwargs):
        img, label = inputs
        mixup_fn = kwargs['mixup_fn']
        if mixup_fn is not None:
            img, label = mixup_fn(img, label)

        outs = self.backbone(img)
        outputs = self.loss(outs, label)
        return outputs

    def test_iter(self, *inputs, **kwargs):
        with paddle.no_grad():
            img, label = inputs
            outs = self.backbone(img)

        return outs

    def infer_iter(self, *inputs, **kwargs):
        with paddle.no_grad():
            outs = self.backbone(*inputs)

        return outs

    def forward(self, *inputs, mode='train', **kwargs):
        if mode == 'train':
            return self.train_iter(*inputs, **kwargs)
        elif mode == 'test':
            return self.test_iter(*inputs, **kwargs)
        elif mode == 'infer':
            return self.infer_iter(*inputs, **kwargs)
        elif mode == 'extract':
            return self.backbone.forward_features(inputs[0])
        else:
            raise Exception("No such mode: {}".format(mode))


def accuracy(output, target, topk=(1, )):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with paddle.no_grad():
        maxk = max(topk)
        if target.dim() > 1:
            target = target.argmax(axis=-1)
        batch_size = target.shape[0]

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = paddle.cast(pred == target.reshape([1, -1]).expand_as(pred),
                              'float32')

        res = []
        for k in topk:
            correct_k = correct[:k].reshape([-1]).sum(0, keepdim=True)
            res.append(correct_k * 100.0 / batch_size)
        return res


class _SoftTargetCrossEntropy(nn.Layer):
    def forward(self, x, target):
        N_rep = x.shape[0]
        N = target.shape[0]
        if not N == N_rep:
            target = target.repeat(N_rep // N, 1)
        loss = paddle.sum(-target * F.log_softmax(x, axis=-1), axis=-1)
        return loss.mean()


class LVViTLoss(nn.Layer):
    """
    Token labeling loss.
    """

    def __init__(self, dense_weight=1.0, cls_weight=1.0, ground_truth=False):
        """
        Constructor Token labeling loss.
        """
        super().__init__()

        self.CE = _SoftTargetCrossEntropy()

        self.dense_weight = dense_weight
        self.cls_weight = cls_weight
        self.ground_truth = ground_truth
        assert dense_weight + cls_weight > 0

    def forward(self, x, target):
        output, aux_output, bb = x
        bbx1, bby1, bbx2, bby2 = bb

        B, N, C = aux_output.shape
        if len(target.shape) == 2:
            target_cls = target
            target_aux = target.repeat([1, N]).reshape([B * N, C])
        else:
            target_cls = target[:, :, 1]
            if self.ground_truth:
                # use ground truth to help correct label.
                # rely more on ground truth if target_cls is incorrect.
                ground_truth = target[:, :, 0]
                ratio = (0.9 - 0.4 *
                         (ground_truth.max(-1)[1] == target_cls.max(-1)[1])
                         ).unsqueeze(-1)
                target_cls = target_cls * ratio + ground_truth * (1 - ratio)
            target_aux = target[:, :, 2:]
            target_aux = target_aux.transpose([0, 2, 1]).reshape([-1, C])
        lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / N)
        if lam < 1:
            target_cls = lam * target_cls + (1 - lam) * target_cls.flip(0)

        aux_output = aux_output.reshape([-1, C])
        loss_cls = self.CE(output, target_cls)
        loss_aux = self.CE(aux_output, target_aux)

        losses = {}
        losses[
            'loss'] = self.cls_weight * loss_cls + self.dense_weight * loss_aux
        losses['acc1'], losses['acc5'] = accuracy(
            output, target_cls, topk=(1, 5))

        return losses

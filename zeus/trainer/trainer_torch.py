# -*- coding: utf-8 -*-

# Copyright (C) 2020. Huawei Technologies Co., Ltd. All rights reserved.
# This program is free software; you can redistribute it and/or modify
# it under the terms of the MIT License.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# MIT License for more details.

"""Torch Trainer."""

import torch
from zeus.trainer.trainer_base import TrainerBase
from zeus.modules.loss import Loss
from zeus.trainer.modules.lr_schedulers import LrScheduler
from zeus.trainer.modules.optimizer import Optimizer
from zeus.common import ClassFactory, ClassType


@ClassFactory.register(ClassType.TRAINER)
class TrainerTorch(TrainerBase):
    """Trainer torch class."""

    def build(self):
        """Build the trainer by assembling the necessary components."""
        super().build()

        self.optimizer = Optimizer()(model=self.model, distributed=self.distributed)
        if hasattr(self.model, 'add_loss'):
            loss_cls = Loss()()
            self.model.add_loss(loss_cls)
            self.loss = self.model.overall_loss()
        else:
            self.loss = Loss()()
        self.lr_scheduler = LrScheduler()(self.optimizer)

        # Some trainer has different train batch size from valid batch
        self.train_metrics = self._init_metrics()
        self.valid_metrics = self._init_metrics()
        self._init_horovod_setting()
        if self.use_amp:
            from apex import amp
            self.model, self.optimizer = amp.initialize(
                self.model, self.optimizer, opt_level='O1')

    def _set_default_funcs(self):
        self.make_batch = self._default_make_batch
        self.train_step = self._default_train_step
        self.valid_step = self._default_valid_step

    def _set_condition(self):
        self._init_distributed_setting()
        torch.manual_seed(self.config.seed)
        self._init_cuda_setting()

    def _init_cuda_setting(self):
        """Init CUDA setting."""
        if not self.config.cuda:
            self.config.device = -1
            return
        self.config.device = self.config.cuda if self.config.cuda is not True else 0
        self.use_cuda = True
        if self.distributed:
            torch.cuda.set_device(self._local_rank_id)
        torch.cuda.manual_seed(self.config.seed)

    def _init_distributed_setting(self):
        if self.distributed:
            import horovod.torch as hvd
            self._world_size = hvd.size()
            self._rank_id = hvd.rank()
            self._local_rank_id = hvd.local_rank()

    def _init_horovod_setting(self):
        """Init horovod setting."""
        self.is_chief = True
        if self.distributed:
            import horovod.torch as hvd
            hvd.broadcast_parameters(self.model.state_dict(), root_rank=0)
            hvd.broadcast_optimizer_state(self.optimizer, root_rank=0)
            if hvd.rank() != 0:
                self.is_chief = False
            else:
                self.is_chief = True

    def _train_epoch(self):
        self.model.train()
        for batch_index, batch in enumerate(self.train_loader):
            batch = self.make_batch(batch)
            batch_logs = {'train_batch': batch}
            self.callbacks.before_train_step(batch_index, batch_logs)
            train_batch_output = self.train_step(batch)
            batch_logs.update(train_batch_output)
            if self.config.is_detection_trainer:
                batch_logs.update({'is_detection_trainer': True})
            self.callbacks.after_train_step(batch_index, batch_logs)

    def _valid_epoch(self):
        self.callbacks.before_valid()
        valid_logs = None

        self.model.eval()
        with torch.no_grad():
            for batch_index, batch in enumerate(self.valid_loader):
                batch = self.make_batch(batch)
                batch_logs = {'valid_batch': batch}
                self.callbacks.before_valid_step(batch_index, batch_logs)
                valid_batch_output = self.valid_step(batch)
                self.callbacks.after_valid_step(batch_index, valid_batch_output)

        self.callbacks.after_valid(valid_logs)

    def _default_make_batch(self, batch):
        """Unpack batch to get input and target."""
        input, target = batch
        if self.use_cuda and not self.config.is_detection_trainer:
            input = self._cuda_from_dict(input)
            target = self._cuda_from_dict(target)
        return (input, target)

    def _cuda_from_dict(self, data):
        if isinstance(data, dict):
            return {k: v.cuda() for k, v in data.items()}
        if isinstance(data, list):
            return [v.cuda() for v in data]
        return data.cuda()

    def _default_train_step(self, batch):
        self.optimizer.zero_grad()
        input, target = batch
        if self.config.is_detection_trainer:
            output = self.model(input, target)
        else:
            output = self.model(input)
        loss = self.loss(output, target)
        if self.use_amp:
            from apex import amp
            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                scaled_loss.backward()
                self.optimizer.synchronize()
            with self.optimizer.skip_synchronize():
                self.optimizer.step()
        else:
            loss.backward()
            if self.config.grad_clip:
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.config.grad_clip)
            self.optimizer.step()
        return {'loss': loss.item(),
                'train_batch_output': output,
                'lr': self.lr_scheduler.get_lr()}

    def _default_valid_step(self, batch):
        input, target = batch
        if self.config.is_detection_trainer:
            output = self.model(input, target)
        else:
            output = self.model(input)
        return {'valid_batch_output': output}

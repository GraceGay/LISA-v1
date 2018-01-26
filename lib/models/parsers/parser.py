#!/usr/bin/env python
# -*- coding: UTF-8 -*-
 
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import time
import tensorflow as tf

from lib.models import nn

from vocab import Vocab
from lib.models.parsers.base_parser import BaseParser

#***************************************************************
class Parser(BaseParser):
  """"""
  
  #=============================================================
  def __call__(self, dataset, moving_params=None):
    """"""

    self.multi_penalties = {k: float(v) for k, v in map(lambda s: s.split(':'), self.multitask_penalties.split(';'))} if self.multitask_penalties else {}
    self.multi_layers = {k: set(map(int, v.split(','))) for k, v in map(lambda s: s.split(':'), self.multitask_layers.split(';'))} if self.multitask_layers else {}

    vocabs = dataset.vocabs
    inputs = dataset.inputs
    targets = dataset.targets
    
    reuse = (moving_params is not None)
    self.tokens_to_keep3D = tf.expand_dims(tf.to_float(tf.greater(inputs[:,:,0], vocabs[0].ROOT)), 2)
    self.sequence_lengths = tf.reshape(tf.reduce_sum(self.tokens_to_keep3D, [1, 2]), [-1,1])
    self.n_tokens = tf.reduce_sum(self.sequence_lengths)
    self.moving_params = moving_params
    
    word_inputs, pret_inputs = vocabs[0].embedding_lookup(inputs[:,:,0], inputs[:,:,1], moving_params=self.moving_params)
    tag_inputs = vocabs[1].embedding_lookup(inputs[:,:,2], moving_params=self.moving_params)
    if self.add_to_pretrained:
      word_inputs += pret_inputs
    if self.word_l2_reg > 0:
      unk_mask = tf.expand_dims(tf.to_float(tf.greater(inputs[:,:,1], vocabs[0].UNK)),2)
      word_loss = self.word_l2_reg*tf.nn.l2_loss((word_inputs - pret_inputs) * unk_mask)
    embed_inputs = self.embed_concat(word_inputs, tag_inputs)
    
    top_recur = embed_inputs
    attn_weights_by_layer = {}

    kernel = 3
    hidden_size = self.num_heads * self.head_size
    print("n_recur: ", self.n_recur)
    print("num heads: ", self.num_heads)
    print("cnn dim: ", self.cnn_dim)
    print("relu hidden size: ", self.relu_hidden_size)
    print("head size: ", self.head_size)

    print("cnn2d_layers: ", self.cnn2d_layers)
    print("cnn_dim_2d: ", self.cnn_dim_2d)

    print("multitask penalties: ", self.multi_penalties)
    print("multitask layers: ", self.multi_layers)

    multitask_targets = {}
    # multitask_outputs = {}

    mask = self.tokens_to_keep3D * tf.transpose(self.tokens_to_keep3D, [0, 2, 1])

    # compute targets adj matrix
    shape = tf.shape(targets[:, :, 1])
    batch_size = shape[0]
    bucket_size = shape[1]
    i1, i2 = tf.meshgrid(tf.range(batch_size), tf.range(bucket_size), indexing="ij")
    idx = tf.stack([i1, i2, targets[:, :, 1]], axis=-1)
    adj = tf.scatter_nd(idx, tf.ones([batch_size, bucket_size]), [batch_size, bucket_size, bucket_size])
    adj = adj * mask

    roots_mask = 1. - tf.expand_dims(tf.eye(bucket_size), 0)

    multitask_losses = {}
    multitask_loss_sum = 0
    multitask_correct = {}

    # normal parse edges
    # multitask_targets['parents'] = adj
    # multitask_targets['children'] = tf.transpose(adj, [0, 2, 1]) * roots_mask

    # create parents targets
    parents = targets[:, :, 1]
    multitask_targets['parents'] = parents

    # create inner sibling targets
    left_inner_sibs = targets[:, :, 3]
    multitask_targets['left_inner_sibs'] = left_inner_sibs
    left_inner_sibs_idx = tf.stack([i1, i2, left_inner_sibs], axis=-1)
    left_inner_sibs_adj = tf.scatter_nd(left_inner_sibs_idx, tf.ones([batch_size, bucket_size]), [batch_size, bucket_size, bucket_size])
    left_inner_sibs_adj = left_inner_sibs_adj * mask

    right_inner_sibs = targets[:, :, 4]
    multitask_targets['right_inner_sibs'] = right_inner_sibs
    right_inner_sibs_idx = tf.stack([i1, i2, right_inner_sibs], axis=-1)
    right_inner_sibs_adj = tf.scatter_nd(right_inner_sibs_idx, tf.ones([batch_size, bucket_size]), [batch_size, bucket_size, bucket_size])
    right_inner_sibs_adj = right_inner_sibs_adj * mask

    self_indices = tf.tile(tf.expand_dims(tf.range(bucket_size), 0), [batch_size, 1])

    inner_sibs = tf.where(tf.not_equal(right_inner_sibs, self_indices), right_inner_sibs, left_inner_sibs)
    multitask_targets['inner_sibs'] = inner_sibs
    inner_sibs_idx = tf.stack([i1, i2, inner_sibs], axis=-1)
    inner_sibs_adj = tf.scatter_nd(inner_sibs_idx, tf.ones([batch_size, bucket_size]), [batch_size, bucket_size, bucket_size])
    inner_sibs_adj = inner_sibs_adj * mask

    # create children targets
    multitask_targets['children'] = tf.transpose(adj, [0, 2, 1]) * roots_mask

    # create grandparents targets
    i1, i2 = tf.meshgrid(tf.range(batch_size), tf.range(bucket_size), indexing="ij")
    idx = tf.reshape(tf.stack([i1, tf.nn.relu(parents)], axis=-1), [-1, 2])
    grandparents = tf.reshape(tf.gather_nd(parents, idx), [batch_size, bucket_size])
    multitask_targets['grandparents'] = grandparents
    grand_idx = tf.stack([i1, i2, grandparents], axis=-1)
    grand_adj = tf.scatter_nd(grand_idx, tf.ones([batch_size, bucket_size]), [batch_size, bucket_size, bucket_size])
    grand_adj = grand_adj * mask


    attn_dropout = 0.67
    prepost_dropout = 0.67
    relu_dropout = 0.67
    # if moving_params is not None:
    #   attn_dropout = 1.0
    #   prepost_dropout = 1.0
    #   relu_dropout = 1.0
    #   self.recur_keep_prob = 1.0

    assert (self.cnn_layers != 0 and self.n_recur != 0) or self.num_blocks == 1, "num_blocks should be 1 if cnn_layers or n_recur is 0"
    assert self.dist_model == 'bilstm' or self.dist_model == 'transformer', 'Model must be either "transformer" or "bilstm"'

    add_attn = tf.zeros([batch_size, bucket_size, bucket_size])

    for b in range(self.num_blocks):
      with tf.variable_scope("block%d" % b, reuse=reuse):  # to share parameters, change scope here
        # Project for CNN input
        if self.cnn_layers > 0:
          with tf.variable_scope('proj0', reuse=reuse):
            top_recur = self.MLP(top_recur, self.cnn_dim, n_splits=1)

        ####### 1D CNN ########
        with tf.variable_scope('CNN', reuse=reuse):
          for i in xrange(self.cnn_layers):
            with tf.variable_scope('layer%d' % i, reuse=reuse):
              if self.cnn_residual:
                top_recur += self.CNN(top_recur, 1, kernel, self.cnn_dim, self.recur_keep_prob, self.info_func)
                top_recur = nn.layer_norm(top_recur, reuse)
              else:
                top_recur = self.CNN(top_recur, 1, kernel, self.cnn_dim, self.recur_keep_prob, self.info_func)
          if self.cnn_residual and self.n_recur > 0:
            top_recur = nn.layer_norm(top_recur, reuse)

        # Project for Tranformer / residual LSTM input
        if self.n_recur > 0:
          if self.dist_model == "transformer":
            with tf.variable_scope('proj1', reuse=reuse):
              top_recur = self.MLP(top_recur, hidden_size, n_splits=1)
          if self.lstm_residual and self.dist_model == "bilstm":
            with tf.variable_scope('proj1', reuse=reuse):
              top_recur = self.MLP(top_recur, (2 if self.recur_bidir else 1) * self.recur_size, n_splits=1)

        ##### Transformer #######
        if self.dist_model == 'transformer':
          with tf.variable_scope('Transformer', reuse=reuse):
            top_recur = nn.add_timing_signal_1d(top_recur)
            for i in range(self.n_recur):
              with tf.variable_scope('layer%d' % i, reuse=reuse):
                # set manual attn
                manual_attn = None
                if self.inject_manual_attn and moving_params is None:
                  if 'parents' in self.multi_layers.keys() and i in self.multi_layers['parents']:
                    manual_attn = adj
                  # top_recur, attn_weights = self.transformer(top_recur, hidden_size, self.num_heads,
                  #                              attn_dropout, relu_dropout, prepost_dropout, self.relu_hidden_size,
                  #                              self.info_func, reuse, manual_attn)
                  elif 'grandparents' in self.multi_layers.keys() and i in self.multi_layers['grandparents']:
                    manual_attn = grand_adj
                  # top_recur, attn_weights = self.transformer(top_recur, hidden_size, self.num_heads,
                  #                                            attn_dropout, relu_dropout, prepost_dropout,
                  #                                            self.relu_hidden_size,
                  #                                            self.info_func, reuse, manual_attn)
                  elif 'inner_sibs' in self.multi_layers.keys() and i in self.multi_layers['inner_siblings']:
                    manual_attn = inner_sibs_adj
                  elif 'left_inner_sibs' in self.multi_layers.keys() and i in self.multi_layers['left_inner_sibs']:
                    manual_attn = left_inner_sibs_adj
                  elif 'right_inner_sibs' in self.multi_layers.keys() and i in self.multi_layers['right_inner_sibs']:
                    manual_attn = right_inner_sibs_adj
                # else:
                top_recur, attn_weights = self.transformer(top_recur, hidden_size, self.num_heads,
                                                           attn_dropout, relu_dropout, prepost_dropout,
                                                           self.relu_hidden_size, self.info_func, reuse, manual_attn, add_attn)
                # head x batch x seq_len x seq_len
                attn_weights_by_layer[i] = tf.transpose(attn_weights, [1, 0, 2, 3])

                if 'parents' in self.multi_layers.keys() and i in self.multi_layers['parents']:
                  add_attn = attn_weights[0]

            # if normalization is done in layer_preprocess, then it should also be done
            # on the output, since the output can grow very large, being the sum of
            # a whole stack of unnormalized layer outputs.
            if self.n_recur > 0:
              top_recur = nn.layer_norm(top_recur, reuse)

        ##### BiLSTM #######
        if self.dist_model == 'bilstm':
          with tf.variable_scope("BiLSTM", reuse=reuse):
            for i in range(self.n_recur):
              with tf.variable_scope('layer%d' % i, reuse=reuse):
                if self.lstm_residual:
                  top_recur_curr, _ = self.RNN(top_recur)
                  top_recur += top_recur_curr
                  # top_recur = nn.layer_norm(top_recur, reuse)
                else:
                  top_recur, _ = self.RNN(top_recur)
            # if self.lstm_residual and self.n_recur > 0:
            #   top_recur = nn.layer_norm(top_recur, reuse)
        if self.num_blocks > 1:
          top_recur = nn.layer_norm(top_recur, reuse)

    ####### 2D CNN ########
    if self.cnn2d_layers > 0:
      with tf.variable_scope('proj2', reuse=reuse):
        top_recur_rows, top_recur_cols = self.MLP(top_recur, self.cnn_dim_2d//2, n_splits=2)
        # top_recur_rows, top_recur_cols = self.MLP(top_recur, self.cnn_dim // 4, n_splits=2)

      top_recur_rows = nn.add_timing_signal_1d(top_recur_rows)
      top_recur_cols = nn.add_timing_signal_1d(top_recur_cols)

      with tf.variable_scope('2d', reuse=reuse):
        # set up input (split -> 2d)
        input_shape = tf.shape(embed_inputs)
        bucket_size = input_shape[1]
        top_recur_rows = tf.tile(tf.expand_dims(top_recur_rows, 1), [1, bucket_size, 1, 1])
        top_recur_cols = tf.tile(tf.expand_dims(top_recur_cols, 2), [1, 1, bucket_size, 1])
        top_recur_2d = tf.concat([top_recur_cols, top_recur_rows], axis=-1)

        # apply num_convs 2d conv layers (residual)
        for i in xrange(self.cnn2d_layers):  # todo pass this in
          with tf.variable_scope('CNN%d' % i, reuse=reuse):
            top_recur_2d += self.CNN(top_recur_2d, kernel, kernel, self.cnn_dim_2d,  # todo pass this in
                                    self.recur_keep_prob if i < self.cnn2d_layers - 1 else 1.0,
                                    self.info_func if i < self.cnn2d_layers - 1 else tf.identity)
            top_recur_2d = nn.layer_norm(top_recur_2d, reuse)

        with tf.variable_scope('Arcs', reuse=reuse):
          arc_logits = self.MLP(top_recur_2d, 1, n_splits=1)
          arc_logits = tf.squeeze(arc_logits, axis=-1)
          arc_output = self.output_svd(arc_logits, targets[:, :, 1])
          if moving_params is None:
            predictions = targets[:, :, 1]
          else:
            predictions = arc_output['predictions']

        # Project each predicted (or gold) edge into head and dep rel representations
        with tf.variable_scope('MLP', reuse=reuse):
          # flat_labels = tf.reshape(predictions, [-1])
          original_shape = tf.shape(arc_logits)
          batch_size = original_shape[0]
          bucket_size = original_shape[1]
          # num_classes = len(vocabs[2])
          i1, i2 = tf.meshgrid(tf.range(batch_size), tf.range(bucket_size), indexing="ij")
          targ = i1 * bucket_size * bucket_size + i2 * bucket_size + predictions
          idx = tf.reshape(targ, [-1])
          conditioned = tf.gather(tf.reshape(top_recur_2d, [-1, self.cnn_dim_2d]), idx)
          conditioned = tf.reshape(conditioned, [batch_size, bucket_size, self.cnn_dim_2d])
          dep_rel_mlp, head_rel_mlp = self.MLP(conditioned, self.class_mlp_size + self.attn_mlp_size, n_splits=2)
    else:
      with tf.variable_scope('MLP', reuse=reuse):
        dep_mlp, head_mlp = self.MLP(top_recur, self.class_mlp_size+self.attn_mlp_size, n_splits=2)
        dep_arc_mlp, dep_rel_mlp = dep_mlp[:,:,:self.attn_mlp_size], dep_mlp[:,:,self.attn_mlp_size:]
        head_arc_mlp, head_rel_mlp = head_mlp[:,:,:self.attn_mlp_size], head_mlp[:,:,self.attn_mlp_size:]

      if self.use_bilinear:
        with tf.variable_scope('Arcs', reuse=reuse):
          arc_logits = self.bilinear_classifier(dep_arc_mlp, head_arc_mlp)
          if 'parents' in self.multi_layers.keys() and self.n_recur-1 in self.multi_layers['parents']:
            arc_logits += attn_weights_by_layer[self.n_recur-1][0]

      else:
        # todo don't hardcode to 0th head
        # todo right now this head is getting 2x loss
        arc_logits = attn_weights_by_layer[self.n_recur-1][0]
        #
        #
        # w1 = 1.0
        # w2 = 1.0
        # w3 = 1.0
        # w4 = 1.0
        # l1 = w1 * attn_weights_by_layer[0][0]
        # l2 = w2 * attn_weights_by_layer[1][0]
        # l3 = w3 * attn_weights_by_layer[2][0]
        # l4 = w4 * attn_weights_by_layer[3][0]
        # arc_logits_all = tf.concat([tf.expand_dims(l1, -1), tf.expand_dims(l2, -1), tf.expand_dims(l3, -1), tf.expand_dims(l4, -1)], -1)
        #
        # arc_logits_all = tf.Print(arc_logits_all, [tf.reduce_max(l1), tf.reduce_max(l2), tf.reduce_max(l3), tf.reduce_max(l4)])
        #
        # # arc_logits_all = tf.Print(arc_logits_all, [tf.shape(attn_weights_by_layer[0][0])], "arc logits", summarize=2000)
        # # arc_logits_all = tf.Print(arc_logits_all, [arc_logits_all], "arc logits all", summarize=2000)
        # # arc_logits = tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[0][0]), tf.int32), targets[:, :, 1]), attn_weights_by_layer[0][0], attn_weights_by_layer[3][0])
        #
        # first_correct = tf.expand_dims(tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[0][0], -1), tf.int32), targets[:, :, 1]), tf.ones([batch_size, bucket_size]), tf.zeros([batch_size, bucket_size])), -1)
        # first_correct_vals = first_correct * attn_weights_by_layer[0][0]
        #
        # # first_correct = tf.Print(first_correct, [first_correct], "first_correct", summarize=2000)
        #
        # # batch x seq_len
        # second_correct = tf.expand_dims(tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[1][0], -1), tf.int32), targets[:, :, 1]), tf.ones([batch_size, bucket_size]), tf.zeros([batch_size, bucket_size])), -1)
        # second_correct_vals = second_correct * attn_weights_by_layer[1][0]
        #
        # # first_correct = tf.Print(first_correct, [second_correct], "second_correct", summarize=2000)
        #
        #
        # third_correct = tf.expand_dims(tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[2][0], -1), tf.int32), targets[:, :, 1]), tf.ones([batch_size, bucket_size]), tf.zeros([batch_size, bucket_size])), -1)
        # third_correct_vals = third_correct * attn_weights_by_layer[2][0]
        #
        # fourth_correct = tf.expand_dims(tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[3][0], -1), tf.int32), targets[:, :, 1]), tf.ones([batch_size, bucket_size]), tf.zeros([batch_size, bucket_size])), -1)
        # fourth_correct_vals = third_correct * attn_weights_by_layer[3][0]
        #
        # # first_correct = tf.Print(first_correct, [third_correct], "third_correct", summarize=2000)
        #
        # arc_logits_all = tf.Print(arc_logits_all, [tf.reduce_sum(first_correct), tf.reduce_sum(second_correct),
        #                                            tf.reduce_sum(third_correct), tf.reduce_sum(fourth_correct)], "correct")
        #
        # # zeros where first, second or third, ones otherwise
        # rest = (1-first_correct) * (1-second_correct) * (1-third_correct)
        # rest_vals = rest * attn_weights_by_layer[3][0]
        #
        # arc_logits = tf.where(tf.not_equal(first_correct_vals, 0), first_correct_vals, attn_weights_by_layer[3][0])
        # arc_logits = tf.where(tf.not_equal(second_correct_vals, 0), second_correct_vals, arc_logits)
        # arc_logits = tf.where(tf.not_equal(third_correct_vals, 0), third_correct_vals, arc_logits)
        #
        # #
        # # arc_logits = tf.Print(arc_logits, [arc_logits], "arc logits all", summarize=2000)
        #
        # arc_logits = tf.reduce_mean(arc_logits_all, -1)
        #
        #
        # # arc_logits = tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[0][1]), tf.int32), targets[:, :, 1]), attn_weights_by_layer[0][2], arc_logits)
        # # arc_logits = tf.where(tf.equal(tf.cast(tf.argmax(attn_weights_by_layer[0][2]), tf.int32), targets[:, :, 1]), attn_weights_by_layer[0][1], arc_logits)

      arc_output = self.output_svd(arc_logits, targets[:, :, 1])
      if moving_params is None:
        predictions = targets[:,:,1]
      else:
        predictions = arc_output['predictions']

    with tf.variable_scope('Rels', reuse=reuse):
      rel_logits, rel_logits_cond = self.conditional_bilinear_classifier(dep_rel_mlp, head_rel_mlp, len(vocabs[2]), predictions)
      rel_output = self.output(rel_logits, targets[:, :, 2])
      rel_output['probabilities'] = self.conditional_probabilities(rel_logits_cond)

    # attn_weights_by_layer[i] = num_heads x seq_len x seq_len for transformer layer i
    # todo pass this in at command line
    # attn_multitask_layer = self.n_recur-1
    # attn_weights = attn_weights_by_layer[attn_multitask_layer]
    for l in sorted(attn_weights_by_layer):
      attn_weights = attn_weights_by_layer[l]
      # attn_weights is: head x batch x seq_len x seq_len
      # idx into attention heads
      attn_idx = 0
      if 'parents' in self.multi_layers.keys() and l in self.multi_layers['parents']:
        outputs = self.output(attn_weights[attn_idx], multitask_targets['parents'])
        attn_idx += 1
        loss = self.multi_penalties['parents'] * outputs['loss']
        multitask_losses['parents%s' % l] = loss
        multitask_correct['parents%s' % l] = outputs['n_correct']
        multitask_loss_sum += loss
      if 'grandparents' in self.multi_layers.keys() and l in self.multi_layers['grandparents']:
        outputs = self.output(attn_weights[attn_idx], multitask_targets['grandparents'])
        attn_idx += 1
        loss = self.multi_penalties['grandparents'] * outputs['loss']
        multitask_losses['grandparents%s' % l] = loss
        multitask_loss_sum += loss
      if 'inner_sibs' in self.multi_layers.keys() and l in self.multi_layers['inner_sibs']:
        outputs = self.output(attn_weights[attn_idx], multitask_targets['inner_sibs'])
        attn_idx += 1
        loss = self.multi_penalties['inner_sibs'] * outputs['loss']
        multitask_losses['inner_sibs%s' % l] = loss
        multitask_loss_sum += loss
      if 'left_inner_sibs' in self.multi_layers.keys() and l in self.multi_layers['left_inner_sibs']:
        outputs = self.output(attn_weights[attn_idx], multitask_targets['left_inner_sibs'])
        attn_idx += 1
        loss = self.multi_penalties['left_inner_sibs'] * outputs['loss']
        multitask_losses['left_inner_sibs%s' % l] = loss
        multitask_loss_sum += loss
      if 'right_inner_sibs' in self.multi_layers.keys() and l in self.multi_layers['right_inner_sibs']:
        outputs = self.output(attn_weights[attn_idx], multitask_targets['right_inner_sibs'])
        attn_idx += 1
        loss = self.multi_penalties['right_inner_sibs'] * outputs['loss']
        multitask_losses['right_inner_sibs%s' % l] = loss
        multitask_loss_sum += loss
      if 'children' in self.multi_layers.keys() and l in self.multi_layers['children']:
        outputs = self.output_multi(attn_weights[attn_idx], multitask_targets['children'])
        attn_idx += 1
        loss = self.multi_penalties['children'] * outputs['loss']
        multitask_losses['children%s' % l] = loss
        multitask_loss_sum += loss


    # for l, attn_weights in attn_weights_by_layer.iteritems():

    # multitask_losses = {'parents': multitask_outputs['parents']['loss'],
    #                     'children': multitask_outputs['children']['loss'],
    #                     'grandparents': multitask_outputs['grandparents']['loss']}
    # # multitask_loss_sum = multitask_outputs['parents']['loss'] + \
    #                      # multitask_outputs['children']['loss'] + \
    #                      # multitask_outputs['grandparents']['loss']
    # multitask_loss_sum = self.multi_penalties['grandparents'] * multitask_outputs['grandparents']['loss'] + self.multi_penalties['parents'] * multitask_outputs['parents']['loss']

    output = {}

    # output['multitask_loss'] = multitask_loss_sum
    output['multitask_losses'] = multitask_losses

    output['probabilities'] = tf.tuple([arc_output['probabilities'],
                                        rel_output['probabilities']])
    output['predictions'] = tf.stack([arc_output['predictions'],
                                      rel_output['predictions']])
    output['correct'] = arc_output['correct'] * rel_output['correct']
    output['tokens'] = arc_output['tokens']
    output['n_correct'] = tf.reduce_sum(output['correct'])
    output['n_tokens'] = self.n_tokens
    output['accuracy'] = output['n_correct'] / output['n_tokens']
    output['loss'] = arc_output['loss'] + rel_output['loss'] + multitask_loss_sum
    if self.word_l2_reg > 0:
      output['loss'] += word_loss

    output['embed'] = embed_inputs
    output['recur'] = top_recur
    # output['dep_arc'] = dep_arc_mlp
    # output['head_dep'] = head_arc_mlp
    output['dep_rel'] = dep_rel_mlp
    output['head_rel'] = head_rel_mlp
    output['arc_logits'] = arc_logits
    output['rel_logits'] = rel_logits

    output['rel_loss'] = rel_output['loss']
    output['log_loss'] = arc_output['log_loss']
    output['2cycle_loss'] = arc_output['2cycle_loss']
    output['roots_loss'] = arc_output['roots_loss']
    output['svd_loss'] = arc_output['svd_loss']
    output['n_cycles'] = arc_output['n_cycles']
    output['len_2_cycles'] = arc_output['len_2_cycles']

    # transpose and softmax attn weights
    attn_weights_by_layer_softmaxed = {k: tf.transpose(tf.nn.softmax(v), [1, 0, 2, 3]) for k, v in attn_weights_by_layer.iteritems()}

    output['attn_weights'] = attn_weights_by_layer_softmaxed

    output['attn_correct'] = multitask_correct

    # output['cycles'] = arc_output['n_cycles'] + arc_output['len_2_cycles']

    #### OLD: TRANSFORMER ####
    # top_recur = nn.add_timing_signal_1d(top_recur)
    #
    # for i in xrange(self.n_recur):
    #   # RNN:
    #   # with tf.variable_scope('RNN%d' % i, reuse=reuse):
    #   #   top_recur, _ = self.RNN(top_recur)
    #
    #   # Transformer:
    #   with tf.variable_scope('Transformer%d' % i, reuse=reuse):
    #     top_recur = self.transformer(top_recur, hidden_size, self.num_heads,
    #                                  attn_dropout, relu_dropout, prepost_dropout, self.relu_hidden_size,
    #                                  self.info_func, reuse)
    # # if normalization is done in layer_preprocess, then it shuold also be done
    # # on the output, since the output can grow very large, being the sum of
    # # a whole stack of unnormalized layer outputs.
    # top_recur = nn.layer_norm(top_recur, reuse)
    #
    # with tf.variable_scope('MLP', reuse=reuse):
    #   dep_mlp, head_mlp = self.MLP(top_recur, self.class_mlp_size+self.attn_mlp_size, n_splits=2)
    #   dep_arc_mlp, dep_rel_mlp = dep_mlp[:,:,:self.attn_mlp_size], dep_mlp[:,:,self.attn_mlp_size:]
    #   head_arc_mlp, head_rel_mlp = head_mlp[:,:,:self.attn_mlp_size], head_mlp[:,:,self.attn_mlp_size:]
    #
    # with tf.variable_scope('Arcs', reuse=reuse):
    #   arc_logits = self.bilinear_classifier(dep_arc_mlp, head_arc_mlp)
    #   # arc_output = self.output(arc_logits, targets[:,:,1])
    #   arc_output = self.output_svd(arc_logits, targets[:,:,1])
    #   if moving_params is None:
    #     predictions = targets[:,:,1]
    #   else:
    #     predictions = arc_output['predictions']
    # with tf.variable_scope('Rels', reuse=reuse):
    #   rel_logits, rel_logits_cond = self.conditional_bilinear_classifier(dep_rel_mlp, head_rel_mlp, len(vocabs[2]), predictions)
    #   rel_output = self.output(rel_logits, targets[:,:,2])
    #   rel_output['probabilities'] = self.conditional_probabilities(rel_logits_cond)
    #
    # output = {}
    # output['probabilities'] = tf.tuple([arc_output['probabilities'],
    #                                     rel_output['probabilities']])
    # output['predictions'] = tf.stack([arc_output['predictions'],
    #                                  rel_output['predictions']])
    # output['correct'] = arc_output['correct'] * rel_output['correct']
    # output['tokens'] = arc_output['tokens']
    # output['n_correct'] = tf.reduce_sum(output['correct'])
    # output['n_tokens'] = self.n_tokens
    # output['accuracy'] = output['n_correct'] / output['n_tokens']
    # output['loss'] = arc_output['loss'] + rel_output['loss']
    # if self.word_l2_reg > 0:
    #   output['loss'] += word_loss
    #
    # output['embed'] = embed_inputs
    # output['recur'] = top_recur
    # output['dep_arc'] = dep_arc_mlp
    # output['head_dep'] = head_arc_mlp
    # output['dep_rel'] = dep_rel_mlp
    # output['head_rel'] = head_rel_mlp
    # output['arc_logits'] = arc_logits
    # output['rel_logits'] = rel_logits
    #
    # output['rel_loss'] = rel_output['loss']
    # output['log_loss'] = arc_output['log_loss']
    # output['2cycle_loss'] = arc_output['2cycle_loss']
    # output['roots_loss'] = arc_output['roots_loss']
    # output['svd_loss'] = arc_output['svd_loss']
    return output
  
  #=============================================================
  def prob_argmax(self, parse_probs, rel_probs, tokens_to_keep, n_cycles=-1, len_2_cycles=-1):
    """"""
    start_time = time.time()
    parse_preds, roots_lt, roots_gt = self.parse_argmax(parse_probs, tokens_to_keep, n_cycles, len_2_cycles)
    rel_probs = rel_probs[np.arange(len(parse_preds)), parse_preds]
    rel_preds = self.rel_argmax(rel_probs, tokens_to_keep)
    total_time = time.time() - start_time
    return parse_preds, rel_preds, total_time, roots_lt, roots_gt

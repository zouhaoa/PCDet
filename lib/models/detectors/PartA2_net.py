import numpy as np
import torch
import torch.nn as nn
import spconv
import torch.nn.functional as F
from .detector3d import Detector3D
from ..model_utils.proposal_layer import proposal_layer
from ...utils import common_utils
from ...config import cfg


class PartA2Net(Detector3D):
    def __init__(self, num_class, target_assigner, output_shape):
        super().__init__(num_class, target_assigner)

        self.output_shape = output_shape
        self.sparse_shape = output_shape + [1, 0, 0]

        self.build_networks(cfg.MODEL)
        self.build_losses(cfg.MODEL.LOSSES)

    def forward_rpn(self, voxels, num_points, coords, batch_size, voxel_centers, **kwargs):
        # RPN inference
        with torch.set_grad_enabled((not cfg.MODEL.RPN.PARAMS_FIXED) and self.training):
            voxel_features = self.vfe(
                features=voxels,
                num_voxels=num_points,
                coords=coords
            )

            input_sp_tensor = spconv.SparseConvTensor(
                features=voxel_features,
                indices=coords,
                spatial_shape=self.sparse_shape,
                batch_size=batch_size
            )

            unet_ret_dict = self.rpn_net(input_sp_tensor, voxel_centers)
            rpn_preds_dict = self.rpn_head(unet_ret_dict['spatial_features'])

        rpn_ret_dict = {
            'rpn_cls_preds': rpn_preds_dict['cls_preds'],
            'rpn_box_preds': rpn_preds_dict['box_preds'],
            'rpn_dir_cls_preds': rpn_preds_dict.get('dir_cls_preds', None),
            'rpn_seg_scores': torch.sigmoid(rpn_preds_dict['u_cls_preds'].view(-1)),
            'rpn_seg_features': rpn_preds_dict['seg_features'],
            'rpn_bev_features': unet_ret_dict['spatial_features'],
            'rpn_part_offsets': torch.sigmoid(unet_ret_dict['u_reg_preds'])
        }
        return rpn_ret_dict

    def forward_rcnn(self, batch_anchors, batch_size, voxel_centers, coords, rpn_ret_dict, input_dict):
        rpn_cls_preds = rpn_ret_dict['rpn_cls_preds']
        rpn_box_preds = rpn_ret_dict['rpn_box_preds']
        rpn_dir_cls_preds = rpn_ret_dict['rpn_dir_cls_preds']

        with torch.no_grad():
            batch_anchors = batch_anchors.view(batch_size, -1, batch_anchors.shape[-1])  # (B, N, 7 + ?)
            num_anchors = batch_anchors.shape[1]
            batch_cls_preds = rpn_cls_preds.view(batch_size, num_anchors, -1)
            batch_box_preds = rpn_box_preds.view(
                batch_size, -1, rpn_box_preds.shape[-1] // self.num_anchors_per_location
            )
            batch_box_preds = self.box_coder.decode_torch(batch_box_preds, batch_anchors)

            if rpn_dir_cls_preds is not None:
                rpn_dir_cls_preds = rpn_dir_cls_preds.view(
                    batch_size, -1, cfg.MODEL.RPN.RPN_HEAD.ARGS['num_direction_bins']
                )
                dir_labels = torch.max(rpn_dir_cls_preds, dim=-1)[1]

                period = (2 * np.pi / cfg.MODEL.RPN.RPN_HEAD.ARGS['num_direction_bins'])
                dir_rot = common_utils.limit_period_torch(
                    batch_box_preds[..., 6] - cfg.MODEL.RPN.RPN_HEAD.ARGS['dir_offset'],
                    cfg.MODEL.RPN.RPN_HEAD.ARGS['dir_limit_offset'], period
                )
                batch_box_preds[..., 6] = dir_rot + cfg.MODEL.RPN.RPN_HEAD.ARGS['dir_offset'] \
                                          + period * dir_labels.to(batch_box_preds.dtype)

            roi_dict = proposal_layer(
                batch_size, batch_cls_preds, batch_box_preds,
                code_size=self.box_coder.code_size, mode=self.mode
            )

        # RCNN inference
        rcnn_input_dict = {
            'voxel_centers': voxel_centers,
            'coordinates': coords,
            'rpn_seg_scores': rpn_ret_dict['rpn_seg_scores'],
            'rpn_seg_features': rpn_ret_dict['rpn_seg_features'],
            'rpn_bev_features': rpn_ret_dict['rpn_bev_features'],
            'rpn_part_offsets': rpn_ret_dict['rpn_part_offsets'],
            'rois': roi_dict['rois'],
            'roi_raw_scores': roi_dict['roi_raw_scores'],
            'roi_labels': roi_dict['roi_labels'],
            'gt_boxes': input_dict.get('gt_boxes', None)
        }

        rcnn_ret_dict = self.rcnn_net.forward(rcnn_input_dict)
        return rcnn_ret_dict

    def forward(self, input_dict):
        batch_anchors = input_dict['anchors']
        batch_size = batch_anchors.shape[0]
        coords = input_dict['coordinates'].int()
        voxel_centers = input_dict['voxel_centers']

        rpn_ret_dict = self.forward_rpn(**input_dict)
        rcnn_ret_dict = self.forward_rcnn(batch_anchors, batch_size, voxel_centers, coords, rpn_ret_dict, input_dict)

        if self.training:
            loss, tb_dict, disp_dict = self.get_training_loss(rpn_ret_dict, rcnn_ret_dict, input_dict)

            ret_dict = {
                'loss': loss
            }
            return ret_dict, tb_dict, disp_dict
        else:
            # prediction mode
            dataset = get_dataset_class(cfg.DATASET)
            batch_rois = batch_roi_labels = batch_roi_raw_scores = batch_rcnn_bbox = None
            batch_gt_boxes = input_dict.get('gt_boxes', None)

            if not cfg.RCNN_STAGE.ENABLED:
                # generate RPN boxes
                batch_anchors = input_dict['anchors'].view(batch_size, -1, input_dict['anchors'].shape[-1])
                assert 'anchors_mask' not in input_dict

                num_class_with_bg = self.num_class + 1 if not cfg.RPN_STAGE.ENCODE_BG_AS_ZEROS else self.num_class
                batch_box_preds = rpn_box_preds.view(batch_size, -1, self.box_coder.code_size)
                batch_cls_preds = rpn_cls_preds.view(batch_size, -1, num_class_with_bg).float()
                batch_box_preds = self.box_coder.decode_torch(batch_box_preds, batch_anchors).float()

                if cfg.RPN_STAGE.RPN_HEAD.USE_DIRECTION_CLASSIFIER:
                    batch_dir_preds = rpn_dir_cls_preds.view(batch_size, -1, cfg.RPN_STAGE.RPN_HEAD.NUM_DIR_BINS)
                    batch_dir_labels = torch.max(batch_dir_preds, dim=-1)[1]

                    period = (2 * np.pi / cfg.RPN_STAGE.RPN_HEAD.NUM_DIR_BINS)
                    dir_rot = box_torch_ops.limit_period(batch_box_preds[..., 6] - cfg.RPN_STAGE.RPN_HEAD.DIR_OFFSET,
                                                         cfg.RPN_STAGE.RPN_HEAD.DIR_LIMIT_OFFSET, period)
                    batch_box_preds[..., 6] = dir_rot + cfg.RPN_STAGE.RPN_HEAD.DIR_OFFSET \
                                             + period * batch_dir_labels.to(batch_box_preds.dtype)

            else:
                # generate RCNN boxes
                rcnn_cls = ret_dict['rcnn_cls']  # (B * N, 1)
                rcnn_reg = ret_dict['rcnn_reg']  # (B * N, C)
                rois = ret_dict['rois']  # (B, N, 7)
                roi_size = rois[:, :, 3:6]
                code_size = self.rcnn_box_coder.code_size

                if cfg.MODEL.LOSSES.RCNN_CLS_LOSS == 'smooth-l1':
                    roi_ry = rois[:, :, 6].view(-1)
                    roi_xyz = rois[:, :, 0:3].view(-1, 3)
                    local_rois = rois.clone().detach()
                    local_rois[:, :, 0:3] = 0
                    rcnn_boxes3d = self.rcnn_box_coder.decode_torch(rcnn_reg.view(local_rois.shape[0], -1, code_size),
                                                                    local_rois).view(-1, code_size)

                    # rcnn_boxes3d[:, [0, 1]] = (rcnn_boxes3d[:, [0, 1]] + rcnn_ret_dict['ans_pred'][:, [0, 1]]) / 2
                    # rcnn_boxes3d[:, -1] = roi_ry + (rcnn_reg[:, -1] + rcnn_ret_dict['ans_pred'][:, -1]) / 2
                    # import pdb
                    # pdb.set_trace()

                    rcnn_boxes3d = box_coder_utils.rotate_pc_along_z_torch(rcnn_boxes3d, (roi_ry + np.pi / 2))
                    rcnn_boxes3d[:, 0:3] += roi_xyz
                    rcnn_boxes3d = rcnn_boxes3d.view(batch_size, -1, code_size)
                else:
                    raise NotImplementedError

                batch_cls_preds = rcnn_cls.view(batch_size, -1)
                batch_box_preds = rcnn_boxes3d
                # batch_box_preds = rois
                # batch_cls_preds = torch.sigmoid(ret_dict['roi_raw_scores'])
                batch_rois = rois
                batch_roi_raw_scores = ret_dict['roi_raw_scores']  # (B, N)
                batch_roi_labels = ret_dict['roi_labels']  # (B, N)

            mode = 'RPN' if not cfg.RCNN_STAGE.ENABLED else 'RCNN'
            pred_dicts, recall_dicts = self.predict(dataset, input_dict, batch_cls_preds, batch_box_preds,
                                                    batch_gt_boxes, batch_rois, batch_roi_labels, batch_roi_raw_scores,
                                                    batch_rcnn_bbox, mode=mode)
            ret_dict.update(recall_dicts)
            return pred_dicts, ret_dict

    def predict(self, dataset, input_dict, batch_cls_preds, batch_box_preds, batch_gt_boxes=None,
                batch_rois=None, batch_roi_labels=None, batch_roi_raw_scores=None, batch_rcnn_bbox=None, mode='RPN'):
        recall_dict = {'roi_05': 0, 'rcnn_05': 0, 'gt': 0, 'roi_07': 0, 'rcnn_07': 0,
                       'rcnn_rank_acc': 0, 'valid_sample_num': 0, 'rcnn_iou_diff': 0}
        predictions_dicts = []

        batch_size = batch_cls_preds.shape[0]
        batch_index = np.arange(batch_size)

        for index, cls_preds, box_preds in zip(batch_index, batch_cls_preds, batch_box_preds):
            if mode == 'RPN':
                if cfg.RPN_STAGE.ENCODE_BG_AS_ZEROS:
                    # this don't support softmax
                    assert cfg.RPN_STAGE.USE_SIGMOID_SCORE is True
                    rank_scores = torch.sigmoid(cls_preds)
                    rank_raw_scores = cls_preds
                else:
                    # encode background as first element in one-hot vector
                    if cfg.RPN_STAGE.USE_SIGMOID_SCORE:
                        rank_scores = torch.sigmoid(cls_preds)[..., 1:]
                    else:
                        rank_scores = F.softmax(cls_preds, dim=-1)[..., 1:]
                    rank_raw_scores = cls_preds[..., 1:]

                if cfg.TEST.USE_MULTI_CLASSES_NMS:
                    top_scores = rank_scores
                    top_raw_scores = rank_raw_scores
                    top_labels = None
                else:
                    top_scores, top_labels = torch.max(rank_scores, dim=-1)
                    top_labels = top_labels + 1  # shift to [1, num_classes]
                    top_raw_scores, _ = torch.max(rank_raw_scores, dim=-1)
            else:
                if batch_gt_boxes is not None:
                    gt_boxes = batch_gt_boxes[index]
                    gt_iou = self.calculate_recall(cls_preds, box_preds, batch_rois[index], gt_boxes, recall_dict)

                    pred_mat = kitti_utils.score_to_compare_matrix(cls_preds)
                    gt_mat = kitti_utils.score_to_compare_matrix(gt_iou)
                    mask = (torch.abs(gt_mat) > 1e-3)
                    fg = ((pred_mat > 0) == (gt_mat > 0))
                    rank_acc = (fg * mask).float().sum() / torch.clamp_min(mask.float().sum(), min=1.0)
                    recall_dict['rcnn_rank_acc'] += rank_acc.item()
                    recall_dict['valid_sample_num'] += (gt_iou.max() > 0).item()
                    iou_diff = torch.abs((torch.sigmoid(cls_preds) - gt_iou))
                    recall_dict['rcnn_iou_diff'] += iou_diff.mean().item()
                else:
                    gt_iou = cls_preds
                rank_raw_scores, rank_scores = self.get_rank_scores(cls_preds, batch_roi_raw_scores[index], gt_iou)

                top_scores = rank_scores.squeeze(-1)
                top_labels = batch_roi_labels[index]
                top_raw_scores = rank_raw_scores

            thresh = torch.tensor([cfg.TEST.SCORE_THRESH], device=rank_scores.device).type_as(rank_scores)
            if cfg.TEST.USE_MULTI_CLASSES_NMS and mode == 'RPN':
                boxes_for_nms = kitti_utils.boxes3d_to_bev_torch_lidar(box_preds)

                selected_list = []
                selected_top_scores = []
                selected_top_raw_scores = []
                selected_labels = []
                num_classes = len(cfg.CLASSES)
                for k in range(0, num_classes):
                    class_scores = rank_scores[:, k]
                    class_scores_keep = class_scores >= thresh

                    if class_scores_keep.int().sum() > 0:
                        original_idxs = class_scores_keep.nonzero().view(-1)
                        cur_boxes_for_nms = boxes_for_nms[class_scores_keep]
                        cur_top_raw_scores = top_raw_scores[class_scores_keep, k]
                        cur_selected = iou3d_utils.nms_gpu(cur_boxes_for_nms, cur_top_raw_scores, cfg.TEST.NMS_THRESH)

                        if cur_selected.shape[0] > 0:
                            selected_list.append(original_idxs[cur_selected])
                            selected_top_scores.append(class_scores[class_scores_keep][cur_selected])
                            selected_top_raw_scores.append(cur_top_raw_scores[cur_selected])
                            selected_labels.append(
                                torch.full([cur_selected.shape[0]], k + 1,
                                           dtype=torch.int64, device=box_preds.device)
                            )

                if selected_list.__len__() >= 1:
                    selected = torch.cat(selected_list, dim=0)
                    selected_labels = torch.cat(selected_labels, dim=0)
                    selected_top_scores = torch.cat(selected_top_scores, dim=0)
                    selected_top_raw_scores = torch.cat(selected_top_raw_scores, dim=0)
                else:
                    selected = []
                    selected_top_scores = top_scores[selected]
                    selected_top_raw_scores = top_raw_scores[selected]
                    selected_labels = selected_top_scores
                selected_boxes = box_preds[selected]
            else:
                top_scores_keep = (top_scores >= thresh)
                top_scores = top_scores.masked_select(top_scores_keep)
                if top_scores.shape[0] > 0:
                    top_raw_scores = top_raw_scores.masked_select(top_scores_keep)
                    box_preds = box_preds[top_scores_keep]

                    # NMS in birdeye view
                    # TODO: use my rotated nms, not checked
                    # boxes_for_nms = kitti_utils.boxes3d_to_bev_torch_lidar(box_preds)
                    # selected = iou3d_utils.nms_gpu(boxes_for_nms, top_raw_scores, cfg.TEST.NMS_THRESH)
                    # selected = selected[:cfg.MAX_OBJECTS_EACH_SCENE]  # max boxes 500

                    top_raw_scores_nms, indices = torch.topk(top_raw_scores,
                                                             k=min(cfg.TEST.FINAL_NMS_PRE_MAXSIZE,
                                                                   top_raw_scores.shape[0]))
                    box_preds_nms = box_preds[indices]
                    boxes_for_nms = kitti_utils.boxes3d_to_bev_torch_lidar(box_preds_nms)#

                    if cfg.TEST.NMS_TYPE == 'rotated':
                        keep_idx = iou3d_utils.nms_gpu(boxes_for_nms, top_raw_scores_nms, cfg.TEST.NMS_THRESH)
                        selected = indices[keep_idx[:cfg.TEST.FINAL_NMS_POST_MAXSIZE]]
                    elif cfg.TEST.NMS_TYPE == 'soft_rotated_nms':
                        import utils.nms.nms_utils as nms_utils
                        top_scores_nms = torch.sigmoid(top_raw_scores_nms)
                        keep_idx, soft_scores = nms_utils.soft_nms_cpu(boxes_for_nms, top_scores_nms,
                                                                       score_thresh=cfg.TEST.SOFTNMS_SCORE_THRESH,
                                                                       sigma=cfg.TEST.NMS_SIGMA,
                                                                       Nt=0.3,
                                                                       soft_type=cfg.TEST.NMS_SOFT_TYPE)
                        selected = indices[keep_idx[:cfg.TEST.FINAL_NMS_POST_MAXSIZE]]
                        top_raw_scores[selected] = top_scores[selected] = soft_scores
                    else:
                        raise NotImplementedError
                    selected = selected[:cfg.MAX_OBJECTS_EACH_SCENE]  # max boxes 500
                else:
                    selected = []

                selected_labels = top_labels[top_scores_keep][selected]
                selected_top_scores = top_scores[selected]
                selected_top_raw_scores = top_raw_scores[selected]
                selected_boxes = box_preds[selected]

            record_dict = {
                'boxes': selected_boxes,
                'scores': selected_top_scores,
                'raw_scores': selected_top_raw_scores,
                'labels': selected_labels
            }

            if mode == 'RCNN':
                record_dict['roi_raw_scores'] = batch_roi_raw_scores[index][top_scores_keep][selected]
                record_dict['rois'] = batch_rois[index][top_scores_keep][selected]
                record_dict['gt_iou'] = gt_iou[top_scores_keep][selected]

                if batch_rcnn_bbox is not None:
                    record_dict['predict_bbox'] = batch_rcnn_bbox[index][top_scores_keep][selected]

                # filter invalid RoIs
                mask = (record_dict['rois'][:, 3:6].sum(dim=1) > 0)
                if mask.sum() != record_dict['rois'].shape[0]:
                    kitti_utils.dict_select(record_dict, mask)

            predictions_dict = dataset.generate_prediction_dict(input_dict, index, record_dict)

            predictions_dicts.append(predictions_dict)
        return predictions_dicts, recall_dict

    def get_training_loss(self, rpn_ret_dict, rcnn_ret_dict, input_dict):
        loss = 0
        tb_dict = {}
        disp_dict = {}
        if not cfg.MODEL.RPN.PARAMS_FIXED:
            rpn_loss, rpn_tb_dict = self.get_rpn_loss(
                u_cls_preds=rpn_ret_dict['u_cls_preds'],
                u_reg_preds=rpn_ret_dict['u_reg_preds'],
                rpn_cls_preds=rpn_ret_dict['rpn_cls_preds'],
                rpn_box_preds=rpn_ret_dict['rpn_box_preds'],
                rpn_dir_cls_preds=rpn_ret_dict['rpn_dir_cls_preds'],
                input_dict=input_dict
            )
            loss += rpn_loss
            tb_dict.update(rpn_tb_dict)

        if not cfg.MODEL.RCNN.ENABLED:
            # RCNN loss
            rcnn_loss, rcnn_tb_dict = self.get_rcnn_loss(rcnn_ret_dict)
            loss += rcnn_loss
            tb_dict.update(rcnn_tb_dict)

            # logging to tensorboard
            rcnn_cls_labels = rcnn_ret_dict['rcnn_cls_labels'].float().view(-1)
            fg_thresh = min(cfg.MODEL.RCNN.ROI_SAMPLER.REG_FG_THRESH, cfg.MODEL.RCNN.ROI_SAMPLER.CLS_FG_THRESH)
            fg_num = (rcnn_cls_labels > fg_thresh).sum().item()
            bg_num = (rcnn_cls_labels == 0).sum().item()
            tb_dict['rcnn_fg_num'] = fg_num
            tb_dict['rcnn_bg_num'] = bg_num

            disp_dict['rcnn_fg_num'] = fg_num

        return loss, tb_dict, disp_dict

    def get_rpn_loss(self, u_cls_preds, u_reg_preds, rpn_cls_preds, rpn_box_preds, rpn_dir_cls_preds, input_dict):
        loss_unet, tb_dict_1 = self.get_unet_loss(
            u_cls_preds=u_cls_preds, u_reg_preds=u_reg_preds, input_dict=input_dict
        )
        loss_anchor_box, tb_dict_2 = self.get_anchor_box_loss(
            cls_preds=rpn_cls_preds, box_preds=rpn_box_preds,
            box_cls_labels=input_dict['box_cls_labels'],
            box_reg_targets=input_dict['box_reg_targets'],
            box_dir_cls_preds=rpn_dir_cls_preds, anchors=input_dict['anchors']
        )
        loss_rpn = loss_unet + loss_anchor_box,
        tb_dict = {
            'loss_rpn': loss_rpn.item(),
            **tb_dict_1,
            **tb_dict_2
        }
        return loss_rpn, tb_dict

    def get_unet_loss(self, u_cls_preds, u_reg_preds, input_dict):
        tb_dict = {}

        # segmentation and part prediction losses
        u_cls_labels, u_reg_labels = input_dict['seg_labels'], input_dict['part_labels']
        u_cls_target = (u_cls_labels > 0).float()
        pos_mask = u_cls_labels > 0
        pos = pos_mask.float()
        neg = (u_cls_labels == 0).float()
        u_cls_weights = pos + neg
        pos_normalizer = pos.sum()
        u_cls_weights = u_cls_weights / torch.clamp(pos_normalizer, min=1.0)
        u_loss_cls = self.rpn_cls_loss_func(u_cls_preds, u_cls_target, weights=u_cls_weights)
        u_loss_cls_pos = (u_loss_cls * pos).sum()
        u_loss_cls_neg = (u_loss_cls * neg).sum()
        u_loss_cls = u_loss_cls.sum()

        loss_unet = u_loss_cls

        if pos_normalizer > 0:
            u_loss_reg = F.binary_cross_entropy(torch.sigmoid(u_reg_preds[pos_mask]), u_reg_labels[pos_mask])
            loss_unet += u_loss_reg
            tb_dict['rpn_u_loss_reg'] = u_loss_reg.item()

        tb_dict['rpn_loss_u_cls'] = u_loss_cls.item()
        tb_dict['rpn_loss_u_cls_pos'] = u_loss_cls_pos.item()
        tb_dict['rpn_loss_u_cls_neg'] = u_loss_cls_neg.item()
        tb_dict['rpn_loss_unet'] = loss_unet.item()
        tb_dict['rpn_pos_num'] = pos_normalizer.item()

        return loss_unet, tb_dict

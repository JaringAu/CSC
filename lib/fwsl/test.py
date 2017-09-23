# -*- coding: utf-8 -*-

from configure import cfg, get_output_dir, get_vis_dir
from utils.timer import Timer
import numpy as np
import cv2
import caffe
from fast_rcnn.nms_wrapper import nms
import cPickle
from utils.im_transforms import get_image_blob
import os
import time
import pprint
from wsl_roi_data_layer.minibatch import get_inner_outer_roi


def _get_rois_blob(im_rois, im_scale_factors):
    """Converts RoIs into network inputs.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob

    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid
    """
    rois, levels = _project_im_rois(im_rois, im_scale_factors)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False), levels


def _get_normalize_roi_blob(img_roi, img_shape, levels):
    img_roi = img_roi.astype(np.float32, copy=False)

    rois_normalized = np.zeros_like(img_roi)
    rois_normalized[:, 0] = img_roi[:, 0] / img_shape[1]
    rois_normalized[:, 1] = img_roi[:, 1] / img_shape[0]
    rois_normalized[:, 2] = img_roi[:, 2] / img_shape[1]
    rois_normalized[:, 3] = img_roi[:, 3] / img_shape[0]

    roi_normalized_blob = np.hstack((levels, rois_normalized))
    return roi_normalized_blob.astype(np.float32, copy=False)


def _get_context_rois_blob(im_rois, im_scale_factors):
    im_inner_rois, im_outer_rois = get_inner_outer_roi(im_rois,
                                                       cfg.CONTEXT_RATIO)

    rois_inner, levels = _project_im_rois(im_inner_rois, im_scale_factors)
    rois_outer, levels = _project_im_rois(im_outer_rois, im_scale_factors)
    rois, levels = _project_im_rois(im_rois, im_scale_factors)

    rois_context_blob = np.hstack((levels, rois_outer, rois))
    rois_frame_blob = np.hstack((levels, rois, rois_inner))

    return rois_context_blob.astype(
        np.float32, copy=False), rois_frame_blob.astype(
            np.float32, copy=False)


def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob

    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (list): image pyramid levels used by each projected RoI
    """
    im_rois = im_rois.astype(np.float32, copy=False)

    if scales.shape[0] > 1:
        print 'current only support one scale each forward'
        exit()

        widths = im_rois[:, 2] - im_rois[:, 0] + 1
        heights = im_rois[:, 3] - im_rois[:, 1] + 1

        areas = widths * heights
        scaled_areas = areas[:, np.newaxis] * (scales[np.newaxis, :]**2)
        diff_areas = np.abs(scaled_areas - 224 * 224)
        levels = diff_areas.argmin(axis=1)[:, np.newaxis]
    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    # rois = im_rois * scales[levels]

    scales_w = scales[:, 1]
    scales_h = scales[:, 0]

    rois02 = im_rois[:, [0, 2]] * scales_w[levels]
    rois13 = im_rois[:, [1, 3]] * scales_h[levels]

    rois = im_rois.copy()
    rois[:, 0] = rois02[:, 0]
    rois[:, 1] = rois13[:, 0]
    rois[:, 2] = rois02[:, 1]
    rois[:, 3] = rois13[:, 1]

    return rois, levels


def _unproject_im_rois(im_rois, scales):
    im_rois = im_rois.astype(np.float32, copy=False)

    if len(scales) > 1:
        print 'current only support one scale each forward'
        exit()

    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    # rois = im_rois / scales[levels]

    scales_w = scales[:, 1]
    scales_h = scales[:, 0]

    rois02 = im_rois[:, [0, 2]] / scales_w[levels]
    rois13 = im_rois[:, [1, 3]] / scales_h[levels]

    rois = im_rois.copy()
    rois[:, 0] = rois02[:, 0]
    rois[:, 1] = rois13[:, 0]
    rois[:, 2] = rois02[:, 1]
    rois[:, 3] = rois13[:, 1]

    return rois


def _get_roi_scores_blob(roi_scores, scale, roi_num):
    roi_scores_blob = np.zeros([0, 1], dtype=np.float)
    for s in scale:
        if roi_scores is not None:
            roi_scores_blob = np.vstack((roi_scores_blob, roi_scores))
        else:
            roi_scores_blob = np.vstack((roi_scores_blob, np.zeros((roi_num,
                                                                    1))))

    roi_scores_blob = np.add(
        np.reshape(roi_scores_blob, [roi_scores_blob.shape[0]]), 1)
    return roi_scores_blob


def _get_roi_num_blob(rois_blob):
    roi_num_blob = np.zeros([0, 1], dtype=np.float)

    num_roi = 0
    idx_im = 0
    for i in range(rois_blob.shape[0]):
        roi = rois_blob[i]
        if roi[0] == idx_im:
            num_roi = num_roi + 1
        else:
            roi_num_blob = np.vstack((roi_num_blob, np.array((num_roi))))
            idx_im = roi[0]
            num_roi = 1

    if num_roi > 0:
        roi_num_blob = np.vstack((roi_num_blob, np.array((num_roi))))
        idx_im = roi[0]
        num_roi = 0

    roi_num_blob = np.reshape(roi_num_blob, [roi_num_blob.shape[0]])

    return roi_num_blob


def _get_blobs(im, rois, roi_scores):
    """Convert an image and RoIs within that image into network inputs."""
    blobs = {'data': None, 'rois': None, 'roi_scores': None}
    blobs['data'], im_scale_factors = get_image_blob(im)
    blobs['rois'], levels = _get_rois_blob(rois, im_scale_factors)
    blobs['rois_normalized'] = _get_normalize_roi_blob(rois, im.shape, levels)
    blobs['roi_scores'] = _get_roi_scores_blob(roi_scores, im_scale_factors,
                                               len(rois))
    if cfg.CONTEXT:
        blobs['rois_context'], blobs['rois_frame'] = _get_context_rois_blob(
            rois, im_scale_factors)
    return blobs, im_scale_factors


def im_detect(net, im, boxes, box_scores):
    """Detect object classes in an image given object proposals.

    Arguments:
        net (caffe.Net): Fast R-CNN network to use
        im (ndarray): color image to test (in BGR order)
        boxes (ndarray): R x 4 array of object proposals or None (for RPN)

    Returns:
        scores (ndarray): R x K array of object class scores (K includes
            background as object category 0)
        boxes (ndarray): R x (4*K) array of predicted bounding boxes
    """
    blobs, im_scales = _get_blobs(im, boxes, box_scores)

    # When mapping from image ROIs to feature map ROIs, there's some aliasing
    # (some distinct image ROIs get mapped to the same feature ROI).
    # Here, we identify duplicate feature ROIs, so we only compute features
    # on the unique subset.
    if cfg.DEDUP_BOXES > 0:
        v = np.array([1, 1e3, 1e6, 1e9, 1e12])
        hashes = np.round(blobs['rois'] * cfg.DEDUP_BOXES).dot(v)
        _, index, inv_index = np.unique(
            hashes, return_index=True, return_inverse=True)
        # print [blobs[key].shape for key in blobs]
        blobs['rois'] = blobs['rois'][index, :]
        blobs['roi_scores'] = blobs['roi_scores'][index]
        # print [blobs[key].shape for key in blobs]
        # print index.shape,inv_index.shape

        if cfg.CONTEXT:
            blobs['rois_context'] = blobs['rois_context'][index, :]
            blobs['rois_frame'] = blobs['rois_frame'][index, :]
    blobs['roi_num'] = _get_roi_num_blob(blobs['rois'])

    # reshape network inputs
    net.blobs['data'].reshape(*(blobs['data'].shape))
    net.blobs['rois_o'].reshape(*(blobs['rois'].shape))
    net.blobs['rois_normalized_o'].reshape(*(blobs['rois_normalized'].shape))
    net.blobs['roi_scores_o'].reshape(*(blobs['roi_scores'].shape))
    if cfg.CONTEXT:
        net.blobs['rois_context_o'].reshape(*(blobs['rois_context'].shape))
        net.blobs['rois_frame_o'].reshape(*(blobs['rois_frame'].shape))
    net.blobs['roi_num_o'].reshape(*(blobs['roi_num'].shape))

    # do forward
    forward_kwargs = {'data': blobs['data'].astype(np.float32, copy=False)}
    forward_kwargs['rois_o'] = blobs['rois'].astype(np.float32, copy=False)
    forward_kwargs['rois_normalized_o'] = blobs['rois_normalized'].astype(
        np.float32, copy=False)
    forward_kwargs['roi_scores_o'] = blobs['roi_scores'].astype(
        np.float32, copy=False)
    if cfg.CONTEXT:
        forward_kwargs['rois_context_o'] = blobs['rois_context'].astype(
            np.float32, copy=False)
        forward_kwargs['rois_frame_o'] = blobs['rois_frame'].astype(
            np.float32, copy=False)
    forward_kwargs['roi_num_o'] = blobs['roi_num'].astype(
        np.float32, copy=False)

    blobs_out = net.forward(**forward_kwargs)

    if cfg.TEST.SVM:
        # use the raw scores before softmax under the assumption they
        # were trained as linear SVMs
        scores = net.blobs['cls_score'].data
    else:
        # use softmax estimated probabilities
        scores = blobs_out['bbox_prob']
        scores = scores.squeeze()

        num_roi_old = boxes.shape[0]
        boxes_old = boxes

        boxes = net.blobs['rois'].data
        # boxes = net.blobs['rois_o'].data
        boxes = boxes[:, 1:]
        boxes[num_roi_old:, :] = _unproject_im_rois(boxes[num_roi_old:, :],
                                                    im_scales)
        boxes[:num_roi_old, :] = boxes_old

    # Simply repeat the boxes, once for each class
    pred_boxes = np.tile(boxes, (1, scores.shape[1]))

    if cfg.DEDUP_BOXES > 0:
        # Map scores and predictions back to the original set of boxes
        scores = scores[inv_index, :]
        # pred_boxes = pred_boxes[inv_index, :]

    return scores, pred_boxes


def vis_heatmap(im, im_ind, class_name, dets, thresh=0.3):
    import matplotlib.pyplot as plt
    heat_map = np.zeros((im.shape[0], im.shape[1]), dtype=np.float32)
    all_score = 0
    for det in dets:
        box = det[:4]
        score = det[4]
        all_score += score
        heat_map[box[1]:box[3], box[0]:box[2]] += score

    if all_score > thresh:
        x = [i for i in range(heat_map.shape[1])]
        y = [i for i in range(heat_map.shape[0])]
        x, y = np.meshgrid(x, y)
        print class_name
        plt.clf()
        plt.autoscale(False, 'both', True)
        axis_max = max(heat_map.shape[0], heat_map.shape[1])
        plt.axis([0, axis_max, 0, axis_max])
        plt.gca().invert_yaxis()
        plt.pcolormesh(x, y, heat_map)
        plt.colorbar()
        # plt.show()
        sv_path = 'tmp/' + str(im_ind) + '_' + str(class_name) + '.png'
        plt.savefig(sv_path)


def vis_detections_highest(im, class_name, dets, thresh=0.3):
    """Visual debugging of detections."""
    import matplotlib.pyplot as plt

    im = im[:, :, (2, 1, 0)]
    score_highest = 0
    highest_id = -1
    for i in xrange(np.minimum(10, dets.shape[0])):
        bbox = dets[i, :4]
        score = dets[i, -1]
        if score > thresh:
            if score > score_highest:
                score_highest = score
                highest_id = i

    if highest_id == -1:
        return

    bbox = dets[highest_id, :4]
    score = dets[highest_id, -1]
    plt.figure()
    plt.cla()
    plt.imshow(im)
    plt.gca().add_patch(
        plt.Rectangle(
            (bbox[0], bbox[1]),
            bbox[2] - bbox[0],
            bbox[3] - bbox[1],
            fill=False,
            edgecolor='g',
            linewidth=3))
    plt.title('{}  {:.3f}'.format(class_name, score))
    # plt.draw()
    plt.show(block=False)


def vis_detections(im, class_name, dets, thresh=0.3):
    """Visual debugging of detections."""
    import matplotlib.pyplot as plt
    im = im[:, :, (2, 1, 0)]
    plt.figure()
    plt.cla()
    plt.imshow(im)
    for i in xrange(np.minimum(10, dets.shape[0])):
        bbox = dets[i, :4]
        score = dets[i, -1]
        if score > thresh:
            plt.gca().add_patch(
                plt.Rectangle(
                    (bbox[0], bbox[1]),
                    bbox[2] - bbox[0],
                    bbox[3] - bbox[1],
                    fill=False,
                    edgecolor='g',
                    linewidth=3))
            # plt.title('{}  {:.3f}'.format(class_name, score))
            plt.draw()
    plt.show()


def apply_nms(all_boxes, thresh):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    nms_boxes = [[[] for _ in xrange(num_images)] for _ in xrange(num_classes)]
    for cls_ind in xrange(num_classes):
        for im_ind in xrange(num_images):
            dets = all_boxes[cls_ind][im_ind]
            if dets == []:
                continue
            # CPU NMS is much faster than GPU NMS when the number of boxes
            # is relative small (e.g., < 10k)
            # TODO(rbg): autotune NMS dispatch
            keep = nms(dets, thresh, force_cpu=True)
            if len(keep) == 0:
                continue
            nms_boxes[cls_ind][im_ind] = dets[keep, :].copy()
    return nms_boxes


def save_debug_im(im, target_size, save_path):
    print 'target_size: ', target_size
    im_shape = im.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    im_scale = float(target_size) / float(im_size_min)
    # Prevent the biggest axis from being more than MAX_SIZE
    if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
        im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
    im_save = cv2.resize(
        im,
        None,
        None,
        fx=im_scale,
        fy=im_scale,
        interpolation=cv2.INTER_LINEAR)
    cv2.imwrite(save_path, im_save)


def test_net(net, imdb, max_per_image=100, thresh=0.000000001, vis=False):
    """Test a network on an image database."""
    print 'max_per_image: ', max_per_image
    print 'thresh: ', thresh

    num_images = len(imdb.image_index)
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(imdb.num_classes)]
    all_scores = [[[] for _ in xrange(num_images)]
                  for _ in xrange(imdb.num_classes)]

    all_boxes_o = [[[] for _ in xrange(num_images)]
                   for _ in xrange(imdb.num_classes)]

    output_dir = get_output_dir(imdb, net)
    if cfg.OPG_DEBUG:
        vis_dir = get_vis_dir(imdb, net)

    # timers
    _t = {'im_detect': Timer(), 'misc': Timer()}

    roidb = imdb.roidb

    test_scales = cfg.TEST.SCALES
    save_id = 0
    for i in xrange(num_images):
        # if imdb.image_index[i] != '001547':
        # continue
        # if i > 100:
        # break
        if vis:
            import matplotlib.pyplot as plt
            # 关闭所有窗口
            # plt.close('all')

        box_proposals = roidb[i]['boxes']
        rois_per_this_image = min(cfg.TEST.ROIS_PER_IM, len(box_proposals))
        box_proposals = box_proposals[0:rois_per_this_image, :]
        if cfg.USE_ROI_SCORE:
            box_scores = roidb[i]['box_scores']
        else:
            box_scores = None

        im = cv2.imread(imdb.image_path_at(i))
        _t['im_detect'].tic()

        scores = None
        boxes = None
        for target_size in test_scales:
            if cfg.OPG_DEBUG:
                save_path = os.path.join(vis_dir, str(save_id) + '_.png')
                save_debug_im(im, target_size, save_path)
                save_id += 1

            cfg.TEST.SCALES = (target_size, )
            scores_scale, boxes_scale = im_detect(net, im, box_proposals,
                                                  box_scores)
            if scores is None:
                scores = scores_scale
                boxes = boxes_scale
            else:
                # TODO(YH): something to do
                scores += scores_scale
                assert np.array_equal(
                    boxes,
                    boxes_scale), 'boxes at each scale should be the same'

            if cfg.OPG_DEBUG:
                os.remove(save_path)

        if cfg.TEST.USE_FLIPPED:
            im_flip = im[:, ::-1, :]
            box_proposals_flip = box_proposals.copy()
            oldx1 = box_proposals_flip[:, 0].copy()
            oldx2 = box_proposals_flip[:, 2].copy()
            box_proposals_flip[:, 0] = im.shape[1] - oldx2 - 1
            box_proposals_flip[:, 2] = im.shape[1] - oldx1 - 1

            for target_size in test_scales:
                boxes_scale_o = boxes_scale
                if cfg.OPG_DEBUG:
                    save_path = os.path.join(vis_dir, str(save_id) + '_.png')
                    save_debug_im(im_flip, target_size, save_path)
                    save_id += 1

                cfg.TEST.SCALES = (target_size, )
                scores_scale, boxes_scale, = im_detect(
                    net, im_flip, box_proposals_flip, box_scores)

                scores += scores_scale

                if cfg.OPG_DEBUG:
                    os.remove(save_path)

        _t['im_detect'].toc()

        _t['misc'].tic()
        # skip j = 0, because it's the background class
        # fuck skip
        for j in xrange(0, imdb.num_classes):
            if 'trainval' in imdb.name:
                if imdb.image_classes_at(i)[j] == 0:
                    all_boxes[j][i] = np.zeros((0, 5), dtype=np.float32)
                    all_boxes_o[j][i] = np.zeros((0, 5), dtype=np.float32)
                    continue

            all_scores[j][i] = sum(scores[:, j])

            inds = np.where(scores[:, j] > thresh)[0]
            cls_scores = scores[inds, j]
            cls_boxes = boxes[inds, j * 4:(j + 1) * 4]
            cls_dets = np.hstack((cls_boxes, cls_scores[:, np.newaxis])) \
                .astype(np.float32, copy=False)
            keep = nms(cls_dets, cfg.TEST.NMS)
            cls_dets = cls_dets[keep, :]
            if vis:
                # vis_heatmap(im, i, imdb.classes[j], cls_dets, thresh=0.3)
                vis_detections_highest(
                    im, imdb.classes[j], cls_dets, thresh=0.3)
                vis_detections(im, imdb.classes[j], cls_dets, thresh=0.03)

            all_boxes[j][i] = cls_dets

            # 保留原始检测结果
            cls_scores_o = scores[:, j]
            cls_boxes_o = boxes[:, j * 4:(j + 1) * 4]
            cls_dets_o = np.hstack((cls_boxes_o, cls_scores_o[:, np.newaxis])) \
                .astype(np.float32, copy=False)
            all_boxes_o[j][i] = cls_dets_o

        # Limit to max_per_image detections *over all classes*
        if max_per_image > 0:
            image_scores = np.hstack(
                [all_boxes[j][i][:, -1] for j in xrange(0, imdb.num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in xrange(0, imdb.num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]
        _t['misc'].toc()

        print 'im_detect: {:d}/{:d} {:.3f}s {:.3f}s' \
            .format(i + 1, num_images, _t['im_detect'].average_time,
                    _t['misc'].average_time)

    if cfg.OPG_DEBUG:
        return

    det_file = os.path.join(output_dir, 'detections.pkl')
    with open(det_file, 'wb') as f:
        cPickle.dump(all_boxes, f, cPickle.HIGHEST_PROTOCOL)

    det_file_o = os.path.join(output_dir, 'detections_o.pkl')
    with open(det_file_o, 'wb') as f:
        cPickle.dump(all_boxes_o, f, cPickle.HIGHEST_PROTOCOL)

    print 'Evaluating detections'
    imdb.evaluate_detections(all_boxes, output_dir, all_scores=all_scores)

# import ray
# from ray import serve
# from ray.serve.handle import DeploymentHandle
import logging
from collections import Counter, defaultdict, deque
import pickle

import os
import glob
import cv2
import numpy as np
import pandas as pd
import torch
from PIL import Image
import lancedb

import clip
from detic import Detic
from detic.inference import load_classifier
from xmem import XMem

from detectron2.structures import Boxes, Instances, pairwise_iou
from torchvision.ops import masks_to_boxes
from torchvision import transforms

from ..util.nms import asymmetric_nms, mask_iou
from ..util.vocab import prepare_vocab
from .download import ensure_db

from IPython import embed

log = logging.getLogger(__name__)

# ray.init()

IMAGENET_DEFAULT_MEAN = (0.485, 0.456, 0.406)
IMAGENET_DEFAULT_STD = (0.229, 0.224, 0.225)


class CustomTrack(XMem.Track):
    hoi_class_id = 0
    state_class_label = ''
    confidence = 0
    def __init__(self, track_id, t_obs, n_init=3, state_history_len=4, hand_obj_history_len=4, **kw):
        super().__init__(track_id, t_obs, n_init, **kw)
        self.label_count = Counter()
        self.obj_state_history = deque(maxlen=state_history_len)
        self.hoi_history = deque(maxlen=hand_obj_history_len)
        self.obj_state_dist = pd.Series(dtype=float)
        self.obj_state_dist_label = None
        self.z_clips = {}

    @property
    def pred_label(self):
        xs = self.label_count.most_common(1)
        return xs[0][0] if xs else None

    def update_state(self, state, pred_label, alpha=0.1):
        # if the label changed, delete the state
        if self.obj_state_dist_label != pred_label:
            self.obj_state_dist = pd.Series(dtype=float)
            self.obj_state_dist_label = pred_label

        # set default
        for k in state.index:
            if k not in self.obj_state_dist:
                self.obj_state_dist[k] = state[k]

        # do EMA
        for k in self.obj_state_dist.index:
            self.obj_state_dist[k] = (1 - alpha) * self.obj_state_dist[k] + alpha * state.get(k, 0)
        return self.obj_state_dist


import itertools
def cat_instances(instance_lists):
    assert all(isinstance(i, Instances) for i in instance_lists)
    assert len(instance_lists) > 0
    if len(instance_lists) == 1:
        return instance_lists[0]

    image_size = instance_lists[0].image_size
    if not isinstance(image_size, torch.Tensor):  # could be a tensor in tracing
        for i in instance_lists[1:]:
            assert i.image_size == image_size
    ret = Instances(image_size)
    for k in instance_lists[0]._fields.keys():
        values = [i.get(k) for i in instance_lists]
        v0 = values[0]
        if isinstance(v0, torch.Tensor):
            values = torch.cat(values, dim=0)
        elif isinstance(v0, list):
            values = list(itertools.chain(*values))
        elif hasattr(type(v0), "cat"):
            values = type(v0).cat(values)
        elif isinstance(v0, np.ndarray):
            values = np.concatenate(values, axis=0)
        else:
            raise ValueError("Unsupported type {} for concatenation".format(type(v0)))
        ret.set(k, values)
    return ret


# IGNORE_CLASSES = ['table', 'dining_table', 'table-tennis_table', 'person']

class ObjectDetector:
    def __init__(
        self, 
        vocabulary, 
        state_db_fname=None, 
        custom_state_clsf_fname=None,
        xmem_config={}, 
        conf_threshold=0.3, 
        detect_hoi=None,
        state_key='state',
        detic_config_key=None,
        additional_roi_heads=None,
        filter_tracked_detections_from_frame=True,
        device='cuda', detic_device=None, egohos_device=None, xmem_device=None, clip_device=None
    ):
        # initialize models
        self.device = device
        self.detic_device = detic_device or device
        self.egohos_device = egohos_device or device
        self.xmem_device = xmem_device or device
        self.clip_device = clip_device or device
        self.detic = Detic([], config=detic_config_key, masks=True, one_class_per_proposal=3, conf_threshold=conf_threshold, device=self.detic_device).eval().to(self.detic_device)

        self.conf_threshold = conf_threshold
        self.filter_tracked_detections_from_frame = filter_tracked_detections_from_frame

        self.egohos = None
        self.egohos_type = np.array(['', 'hand', 'hand', 'obj', 'obj', 'obj', 'obj', 'obj', 'obj', 'cb'])
        self.egohos_hand_side = np.array(['', 'left', 'right', 'left', 'right', 'both', 'left', 'right', 'both', ''])
        if detect_hoi is not False:
            try:
                from egohos import EgoHos
                self.egohos = EgoHos('obj1', device=self.egohos_device).eval()
            except ImportError as e:
                print('Could not import EgoHOS:', e)
                if detect_hoi is True:
                    raise

        self.xmem = XMem({
            'top_k': 30,
            'mem_every': 30,
            'deep_update_every': -1,
            'enable_long_term': True,
            'enable_long_term_count_usage': True,
            'num_prototypes': 128,
            'min_mid_term_frames': 6,
            'max_mid_term_frames': 12,
            'max_long_term_elements': 1000,
            'tentative_frames': 3,
            'tentative_age': 3,
            'max_age': 60,  # in steps
            # 'min_iou': 0.3,
            **xmem_config,
        }, Track=CustomTrack).to(self.xmem_device).eval()

        # load vocabularies
        if vocabulary.get('base'):
            _, open_meta, _ = load_classifier(vocabulary['base'], prepare=False)
            base_prompts = open_meta.thing_classes
        else:
            base_prompts = []
        tracked_prompts, tracked_vocab = prepare_vocab(vocabulary['tracked'])
        untracked_prompts, untracked_vocab = prepare_vocab(vocabulary.get('untracked') or [])

        # get base prompts
        remove_vocab = set(vocabulary.get('remove') or ()) | set(tracked_prompts) | set(untracked_prompts)
        base_prompts = [c for c in base_prompts if c not in remove_vocab]

        # get base vocab
        equival_map = vocabulary.get('equivalencies') or {}
        base_vocab = [equival_map.get(c, c) for c in base_prompts]

        # combine and get final vocab list
        full_vocab = list(tracked_vocab) + list(untracked_vocab) + base_vocab
        full_prompts = list(tracked_prompts) + list(untracked_prompts) + base_prompts

        # if external_vocab:
        #     full_vocab, full_prompts = list(zip(*[(v, p) for v, p in zip(full_vocab, full_prompts) if v not in external_vocab])) or [[],[]]
        
        if additional_roi_heads is not None and not isinstance(additional_roi_heads, list):
            additional_roi_heads = [additional_roi_heads]
        self.additional_roi_heads = [
            (torch.load(h) if isinstance(h, str) else h).to(self.detic_device)
            for h in additional_roi_heads or []
        ]
        for h in self.additional_roi_heads:
            h.one_class_per_proposal = self.detic.predictor.model.roi_heads.one_class_per_proposal
            # for p in h.box_predictor:
                # p.test_topk_per_image = self.detic.predictor.model.roi_heads.box_predictor[0].test_topk_per_image
        self.additional_roi_heads_labels = [h.labels for h in self.additional_roi_heads]
        labels_covered_by_roi_heads = [l for ls in self.additional_roi_heads_labels for l in ls]
        self.base_labels = [l for l in full_vocab if l not in labels_covered_by_roi_heads]


        self.tracked_vocabulary = np.asarray(list(set(tracked_vocab)))
        self.ignored_vocabulary = np.asarray(['IGNORE'])

        self.skill_clsf, _, _ = load_classifier(full_prompts, metadata_name='lvis+', device=self.detic_device)
        self.skill_labels = np.asarray(full_vocab)
        self.skill_labels_is_tracked = np.isin(self.skill_labels, self.tracked_vocabulary)
        self.state_ema = 0.25


        self.state_clsf_type = None
        self.state_db_key = state_key
        self.obj_label_names = []
        self.sklearn_state_clsfs = {}
        if state_db_fname:
            if state_db_fname.endswith(".lancedb"):
                self.state_clsf_type = 'lancedb'
                # image encoder
                self.clip, self.clip_pre = clip.load("ViT-B/32", device=self.clip_device)

                state_db_fname = ensure_db(state_db_fname)
                print("Using state db:", state_db_fname)
                self.obj_state_db = lancedb.connect(state_db_fname)
                self.obj_label_names = self.obj_state_db.table_names()
                self.obj_state_tables = {
                    k: self.obj_state_db[k]
                    for k in self.obj_label_names
                }
                print(f"State DB: {self.obj_state_db}")
                print(f'Objects: {self.obj_label_names}')
                # for name in self.obj_label_names:
                #     tbl.create_index(num_partitions=256, num_sub_vectors=96)

        if custom_state_clsf_fname:
            import joblib
            for f in glob.glob(os.path.join(custom_state_clsf_fname, '*.joblib')):
                cname = os.path.splitext(os.path.basename(f))[0]
                print('using sklearn model:', cname, f)
                c = joblib.load(f)
                c.labels = np.array([l.strip() for l in open(os.path.join(custom_state_clsf_fname, f'{cname}.txt')).readlines() if l.strip()])
                self.sklearn_state_clsfs[cname] = c
                print(c)
        # print(self.sklearn_state_clsfs)
        # input()
        # embed()


            # if state_db_fname.endswith('.pkl'):
            #     self.state_clsf_type = 'dino'
            #     self.dinov2 = torch.hub.load('facebookresearch/dinov2', 'dinov2_vits14_reg').eval().to(self.clip_device)
            #     self.dino_head, self.dino_classes = pickle.load(open(state_db_fname, 'rb'))

            #     dino_object_classes = np.array([x.split('__')[0] for x in self.dino_classes])
            #     self.dino_state_classes = np.array([x.split('__')[1] for x in self.dino_classes])
            #     self.obj_label_names = np.unique(dino_object_classes)
            #     self.dino_label_mask = {l: dino_object_classes == l for l in self.obj_label_names}

            #     self.dino_pre = transforms.Compose([
            #         transforms.Resize(224, interpolation=transforms.InterpolationMode.BICUBIC),
            #         transforms.CenterCrop(224),
            #         transforms.ToTensor(),
            #         transforms.Normalize(mean=IMAGENET_DEFAULT_MEAN, std=IMAGENET_DEFAULT_STD),
            #     ])
            #     print(f'Objects: {self.obj_label_names}')

    def clear_memory(self):
        self.xmem.clear_memory()

    def predict_objects(self, image):
        # ----------------------------- Object Detection ----------------------------- #

        # predict objects
        detic_query = self.detic.build_query(image)
        outputs = detic_query.detect(self.skill_clsf, conf_threshold=0.3, labels=self.skill_labels)
        instances = outputs['instances']
        if self.additional_roi_heads:
            instances = instances[np.isin(instances.pred_labels, self.base_labels)]
            instances_list = [
                detic_query.detect(self.skill_clsf, roi_heads=h, labels=self.skill_labels)['instances']
                for h in self.additional_roi_heads
            ]
            instances_list = [
                h[np.isin(h.pred_labels, ls)]
                for h, ls in zip(instances_list, self.additional_roi_heads_labels)
            ]
            instances = self._cat_instances(instances, instances_list)
        instances = self._filter_detections(instances)
        return instances, detic_query
    
    def _cat_instances(self, instances, instances_list):
        if instances_list:
            instances = [instances] + instances_list
            # score_len = max(x.topk_scores.shape[1] for x in instances)
            # print(score_len)
            # class_offset = 0
            # for x in instances:
            #     try:
            #         x.remove('topk_scores')
            #         x.remove('topk_classes')
            #         x.remove('topk_labels')
            #     except KeyError:
            #         pass
            #     s = x.pred_scores
            #     x.pred_scores = torch.cat([torch.zeros((len(s), class_offset), device=s.device, dtype=s.dtype), s], dim=1)
            #     class_offset += s.shape[1]

                # s = x.topk_scores
                # if s.shape[1] < score_len:
                #     print(s.shape)
                #     x2 = torch.zeros((len(x), score_len), device=s.device, dtype=s.dtype)
                #     x2[:, :len(s)] = s
                #     x.topk_scores = x2
            instances = cat_instances(instances)
        return instances
    
    def _filter_detections(self, instances):
        # drop any ignored instances
        instances = instances[~np.isin(instances.pred_labels, self.ignored_vocabulary)]
        # filter out objects completely inside another object
        obj_priority = torch.from_numpy(np.isin(instances.pred_labels, self.tracked_vocabulary)).int()
        filtered, overlap = asymmetric_nms(instances.pred_boxes.tensor, instances.scores, obj_priority, iou_threshold=0.85)
        filtered_instances = instances[filtered.cpu().numpy()]
        # if Counter(instances.pred_labels.tolist()).get('tortilla', 0) > 1:
        #     embed()
        for i, i_ov in enumerate(overlap):
            if not len(i_ov): continue
            # get overlapping instances
            overlap_insts = instances[i_ov.cpu().numpy()]
            log.info(f"object {filtered_instances.pred_labels[i]} filtered {overlap_insts.pred_labels}")

            # merge overlapping detections with the same label
            overlap_insts = overlap_insts[overlap_insts.pred_labels == filtered_instances.pred_labels[i]]
            if len(overlap_insts):
                log.info(f"object {filtered_instances.pred_labels[i]} merged {len(overlap_insts)}")
                filtered_instances.pred_masks[i] |= torch.maximum(
                    filtered_instances.pred_masks[i], 
                    overlap_insts.pred_masks.max(0).values)
            # filtered_instances.pred_masks
        # log.info("filtered detections %s", len(filtered_instances))
        return filtered_instances

    def predict_hoi(self, image):
        if self.egohos is None:
            return None, None
        # -------------------------- Hand-Object Interaction ------------------------- #

        # predict HOI
        hoi_masks, hoi_class_ids = self.egohos(image)
        keep = hoi_masks.sum(1).sum(1) > 4
        hoi_masks = hoi_masks[keep]
        hoi_class_ids = hoi_class_ids[keep.cpu().numpy()]
        # create detectron2 instances
        instances = Instances(
            image.shape,
            pred_masks=hoi_masks,
            pred_boxes=Boxes(masks_to_boxes(hoi_masks)),
            pred_hoi_classes=hoi_class_ids)
        # get a mask of the hands
        hand_mask = hoi_masks[self.egohos_type[hoi_class_ids] == 'hand'].sum(0)
        return instances, hand_mask

    def merge_hoi(self, other_detections, hoi_detections, detic_query):
        if hoi_detections is None:
            return None
        is_obj_type = self.egohos_type[hoi_detections.pred_hoi_classes] == 'obj'
        hoi_obj_detections = hoi_detections[is_obj_type]
        hoi_obj_masks = hoi_obj_detections.pred_masks
        hoi_obj_boxes = hoi_obj_detections.pred_boxes.tensor
        hoi_obj_hand_side = self.egohos_hand_side[hoi_detections.pred_hoi_classes[is_obj_type]]


        # ----------------- Compare & Merge HOI with Object Detector ----------------- #

        # get mask iou
        other_detections = [d for d in other_detections if d is not None]
        mask_list = [d.pred_masks.to(self.egohos_device) for d in other_detections]
        det_masks = torch.cat(mask_list) if mask_list else torch.zeros(0, hoi_obj_masks.shape[1:])
        iou = mask_iou(det_masks, hoi_obj_masks)
        # add hand side interaction to tracks
        i = 0
        for d, b in zip(other_detections, mask_list):
            d.left_hand_interaction = iou[i:i+len(b), hoi_obj_hand_side == 'left'].sum(1)
            d.right_hand_interaction = iou[i:i+len(b), hoi_obj_hand_side == 'right'].sum(1)
            d.both_hand_interaction = iou[i:i+len(b), hoi_obj_hand_side == 'both'].sum(1)
            i += len(b)

        # ---------------------- Predict class for unlabeled HOI --------------------- #

        # get hoi objects with poor overlap
        hoi_iou = iou.sum(0)
        hoi_is_its_own_obj = hoi_iou < 0.2

        bbox = hoi_obj_boxes[hoi_is_its_own_obj].to(self.detic_device)
        masks = hoi_obj_masks[hoi_is_its_own_obj]
        scores = torch.Tensor(1 - hoi_iou[hoi_is_its_own_obj])
        labels = np.array(['unknown' for i in range(len(bbox))])
        try:
            instances = Instances(
                hoi_obj_detections.image_size,
                scores=scores,
                pred_boxes=Boxes(bbox),
                pred_masks=masks,
                pred_labels=labels,
            )
        except AssertionError:
            print('failed creating unknown instances:\n', bbox.shape, masks.shape, scores.shape, labels.shape, '\n')
            instances = None
        return instances

        # # get labels for HOIs
        # hoi_outputs = detic_query.predict(
        #     hoi_obj_boxes[hoi_is_its_own_obj].to(self.detic_device), 
        #     self.skill_clsf, labels=self.skill_labels)

        # hoi_detections2 = hoi_outputs['instances']
        # hoi_detections2.pred_labels[:] = 'unknown'
        # pm = hoi_obj_detections.pred_masks[hoi_is_its_own_obj]
        # # if len(hoi_detections2) != len(pm):
        # #     print(len(hoi_detections2))
        # #     print(hoi_is_its_own_obj)
        # #     print(pm.shape)
        # hoi_detections2.pred_masks = pm
        # hoi_is_its_own_obj = hoi_is_its_own_obj.cpu()
        # hoi_detections2.left_hand_interaction = torch.as_tensor(hoi_obj_hand_side == 'left')[hoi_is_its_own_obj]
        # hoi_detections2.right_hand_interaction = torch.as_tensor(hoi_obj_hand_side == 'right')[hoi_is_its_own_obj]
        # hoi_detections2.both_hand_interaction = torch.as_tensor(hoi_obj_hand_side == 'both')[hoi_is_its_own_obj]
        # # TODO: add top K classes and scores
        # return hoi_detections2

    def filter_objects(self, detections):
        return detections, detections

    def track_objects(self, image, detections, negative_mask=None):
        # 
        det_mask = None
        det_scores = None
        if detections is not None:
            # other_mask = frame_detections.pred_masks
            det_scores = detections.pred_scores
            det_mask = detections.pred_masks.to(self.xmem_device)
        if negative_mask is not None:
            negative_mask = negative_mask.to(self.xmem_device)

        # run xmem
        pred_mask, track_ids, input_track_ids = self.xmem(
            image, det_mask, 
            negative_mask=negative_mask, 
            mask_scores=det_scores,
            tracked_labels=self.skill_labels_is_tracked,
            only_confirmed=True
        )
        # update label counts
        tracks = self.xmem.tracks
        if input_track_ids is not None and detections is not None:
            labels = detections.pred_labels
            scores = detections.scores
            for i, ti in enumerate(input_track_ids):
                if ti >= 0:
                    tracks[ti].label_count.update([labels[i]])
                    tracks[ti].confidence = scores[i]

        instances = Instances(
            image.shape,
            scores=torch.Tensor([tracks[i].confidence for i in track_ids]),
            pred_boxes=Boxes(masks_to_boxes(pred_mask)),
            pred_masks=pred_mask,
            pred_labels=np.array([tracks[i].pred_label for i in track_ids]),
            track_ids=torch.as_tensor(track_ids),
        )

        frame_detections = detections
        if detections is not None and self.filter_tracked_detections_from_frame:
            frame_detections = detections[~np.isin(detections.pred_labels, self.tracked_vocabulary)]
        return instances, frame_detections

    def predict_state(self, image, detections, det_shape=None):
        states = []

        labels = detections.pred_labels
        has_state = np.isin(labels, self.obj_label_names)
        track_ids = detections.track_ids.cpu().numpy() if detections.has('track_ids') else None
        dets = detections[has_state]
        i_z = {k: i for i, k in enumerate(np.where(has_state)[0])}
        Z_imgs = self._encode_boxes(image, dets.pred_boxes.tensor, det_shape=det_shape) if len(dets) else None
        for i in range(len(detections)):
            pred_label = labels[i]
            state = {}

            if has_state[i]:
                if pred_label in self.sklearn_state_clsfs:
                    z = Z_imgs[i_z[i]].cpu().numpy()
                    c = self.sklearn_state_clsfs[pred_label]
                    y = c.predict_proba(z[None])[0]
                    state = {
                        c: x for c, x in zip(c.labels.tolist(), y.tolist())
                    }
                    # print(pred_label)
                    # print(sorted(state.items(), key=lambda x: x[1])[-3:])
                    # input()

                elif self.state_clsf_type == 'lancedb':
                    z = Z_imgs[i_z[i]].cpu().numpy()
                    df = self.obj_state_tables[pred_label].search(z).limit(11).to_df()
                    state = df[self.state_db_key].value_counts()
                    state = state / state.sum()
                    if track_ids is not None and track_ids[i] in self.xmem.tracks:
                        state = self.xmem.tracks[track_ids[i]].update_state(state, pred_label, self.state_ema)
                    state = state.to_dict()
                # elif self.state_clsf_type == 'dino':
                #     y = Z_imgs[i_z[i]]#.cpu().numpy()
                #     assert y.shape[-1] == self.dino_state_classes.shape[0]
                #     label_mask = self.dino_label_mask[pred_label]
                #     state = dict(zip(
                #         self.dino_state_classes[label_mask].tolist(),
                #         y[label_mask].tolist()
                #     ))
                #     # print(state)

            states.append(state)
        # detections.__dict__['pred_states'] = states
        detections.pred_states = np.array(states)
        return detections

    def _encode_boxes(self, img, boxes, det_shape=None):
        # BGR
        # encode each bounding box crop with clip
        # print(f"Clip encoding: {img.shape} {boxes.shape}")
        # for x, y, x2, y2 in boxes.cpu():
        #     Image.fromarray(img[
        #         int(y):max(int(np.ceil(y2)), int(y+2)),
        #         int(x):max(int(np.ceil(x2)), int(x+2)),
        #         ::-1]).save("box.png")
        #     input()
        sx = sy = 1
        if det_shape:
            hd, wd = det_shape[:2]
            hi, wi = img.shape[:2]
            sx = wi / wd
            sy = hi / hd
        crops = [
            Image.fromarray(img[
                max(int(y * sy - 15), 0):max(int(np.ceil(y2 * sy + 15)), int(y * sy + 2)),
                max(int(x * sx - 15), 0):max(int(np.ceil(x2 * sx + 15)), int(x * sx + 2)),
                ::-1])
            for x, y, x2, y2 in boxes.cpu()
        ]
        # for c in crops:
        #     c.save('demo.png')
        #     input()

        if self.state_clsf_type == 'lancedb':
            Z = self.clip.encode_image(torch.stack([self.clip_pre(x) for x in crops]).to(self.clip_device))
            # Z /= Z.norm(dim=1, keepdim=True)
        # elif self.state_clsf_type == 'dino':
        #     Z = self.dinov2(torch.stack([self.dino_pre(x) for x in crops]).to(self.clip_device))
        #     Z = self.dino_head.predict_proba(np.ascontiguousarray(Z.cpu().numpy()))
        return Z
    
    def classify(self, Z, labels):
        outputs = []
        for z, l in zip(Z, labels):
            z_cls, txt_cls = self.classifiers[l]
            out = (z @ z_cls.t()).softmax(dim=-1).cpu().numpy()
            i = np.argmax(out)
            outputs.append(txt_cls[i])
        return np.atleast_1d(np.array(outputs))

    def forward(self, img, boxes, labels):
        valid = self.can_classify(labels)
        if not valid.any():
            return np.array([None]*len(boxes))
        labels = np.asanyarray(labels)
        Z = self.encode_boxes(img, boxes[valid])
        clses = self.classify(Z, labels[valid])
        all_clses = np.array([None]*len(boxes))
        all_clses[valid] = clses
        return all_clses


class Perception:
    def __init__(self, *a, detect_every_n_seconds=0.5, max_width=480, **kw):
        self.detector = ObjectDetector(*a, **kw)
        self.detect_every_n_seconds = 0 if detect_every_n_seconds is True else detect_every_n_seconds
        self.detection_timestamp = -1e30
        self.max_width = max_width

    def clear_memory(self):
        self.detector.clear_memory()
        self.detection_timestamp = -1e30

    @torch.no_grad()
    def predict(self, image, timestamp):
        # # Get a small version of the image
        # h, w = image.shape[:2]
        full_image = image
        # W = self.max_width
        # H = int((h * W / w)//16)*16
        # # W = int((w * H / h)//16)*16
        # image = cv2.resize(image, (W, H))

        # ---------------------------------------------------------------------------- #
        #                           Detection: every N frames                          #
        # ---------------------------------------------------------------------------- #

        detections = detic_query = hoi_detections = hand_mask = None
        is_detection_frame = abs(timestamp - self.detection_timestamp) >= self.detect_every_n_seconds
        if is_detection_frame:
            self.detection_timestamp = timestamp

            # -------------------------- First we detect objects ------------------------- #
            # Detic: 

            detections, detic_query = self.detector.predict_objects(image)

            # ------------------ Then we detect hand object interactions ----------------- #
            # EgoHOS:

            hoi_detections, hand_mask = self.detector.predict_hoi(image)

        # ---------------------------------------------------------------------------- #
        #                             Tracking: Every frame                            #
        # ---------------------------------------------------------------------------- #

        # ------------------------- Then we track the objects ------------------------ #
        # XMem:

        track_detections, frame_detections = self.detector.track_objects(image, detections, negative_mask=hand_mask)

        # ---------------------------------------------------------------------------- #
        #                            Predicting Object State                           #
        # ---------------------------------------------------------------------------- #

        # -------- For objects with labels we care about, classify their state ------- #
        # LanceDB:

        # predict state for tracked objects
        track_detections = self.detector.predict_state(full_image, track_detections, image.shape)
        # predict state for untracked objects
        # if frame_detections is not None:
        #     frame_detections = self.detector.predict_state(image, frame_detections)


        # ----- Merge our multi-model detections into a single set of detections ----- #
        # IoU between tracks+frames & hoi:

        if hoi_detections is not None:
            # Merging HOI into track_detections, frame_detections, hoi_detections
            hoi_detections = self.detector.merge_hoi(
                [track_detections, frame_detections],
                hoi_detections,
                detic_query)

        self.timestamp = timestamp
        return track_detections, frame_detections, hoi_detections


    def serialize_detections(self, detections, frame_shape, include_mask=False):
        if detections is None:
            return None
        bboxes = detections.pred_boxes.tensor.cpu().numpy()
        bboxes[:, 0] /= frame_shape[1]
        bboxes[:, 1] /= frame_shape[0]
        bboxes[:, 2] /= frame_shape[1]
        bboxes[:, 3] /= frame_shape[0]
        labels = detections.pred_labels
        track_ids = detections.track_ids.cpu().numpy() if detections.has('track_ids') else None

        scores = detections.scores.cpu().numpy() if detections.has('scores') else None

        hand_object = { k: f'{k}_hand_interaction' for k in ['left', 'right', 'both'] }
        hand_object = {
            k: detections.get(kk).cpu().numpy()
            for k, kk in hand_object.items() 
            if detections.has(kk)}

        possible_labels = None
        if detections.has('topk_scores'):
            possible_labels = [
                {k: v for k, v in zip(ls.tolist(), ss.tolist()) if v > 0}
                for ls, ss in zip(detections.topk_labels, detections.topk_scores.cpu().numpy())
            ]

        segments = None
        if include_mask and detections.has('pred_masks'):
            segments = [
                norm_contours(cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)[0], frame_shape)
                for mask in detections.pred_masks.cpu().numpy().astype(np.uint8)
            ]

        states = detections.pred_states if detections.has('pred_states') else None

        output = []
        for i in range(len(detections)):
            data = {
                'xyxyn': bboxes[i].tolist(),
                'label': labels[i],
            }

            if scores is not None:
                data['confidence'] = scores[i]

            if hand_object:
                data['hand_object'] = ho = {k: x[i] for k, x in hand_object.items()}
                data['hand_object_interaction'] = max(ho.values(), default=0)

            if possible_labels:
                data['possible_labels'] = possible_labels[i]

            if segments:
                data['segment'] = segments[i]

            if states is not None:
                data['state'] = states[i]

            if track_ids is not None:
                data['segment_track_id'] = track_ids[i]

            output.append(data)
        return output


def norm_contours(contours, shape):
    contours = list(contours)
    WH = np.array(shape[:2][::-1])
    for i in range(len(contours)):
        contours[i] = np.asarray(contours[i]) / WH
    return contours

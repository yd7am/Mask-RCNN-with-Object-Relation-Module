from typing import List, Optional, Dict, Tuple

import torch
from torch import nn, Tensor
from torch.nn import functional as F
import torchvision

from . import det_utils
from . import boxes as box_ops
from .image_list import ImageList


@torch.jit.unused
def _onnx_get_num_anchors_and_pre_nms_top_n(ob, orig_pre_nms_top_n):
    # type: (Tensor, int) -> Tuple[int, int]
    from torch.onnx import operators
    num_anchors = operators.shape_as_tensor(ob)[1].unsqueeze(0)
    pre_nms_top_n = torch.min(torch.cat(
        (torch.tensor([orig_pre_nms_top_n], dtype=num_anchors.dtype),
         num_anchors), 0))

    return num_anchors, pre_nms_top_n


class AnchorsGenerator(nn.Module):
    __annotations__ = {
        "cell_anchors": Optional[List[torch.Tensor]],
        "_cache": Dict[str, List[torch.Tensor]]
    }

    """
    anchors生成器
    Module that generates anchors for a set of feature maps and
    image sizes.

    The module support computing anchors at multiple sizes and aspect ratios
    per feature map.

    sizes and aspect_ratios should have the same number of elements, and it should
    correspond to the number of feature maps.

    sizes[i] and aspect_ratios[i] can have an arbitrary number of elements,
    and AnchorGenerator will output a set of sizes[i] * aspect_ratios[i] anchors
    per spatial location for feature map i.

    Arguments:
        sizes (Tuple[Tuple[int]]):
        aspect_ratios (Tuple[Tuple[float]]):
    """

    def __init__(self, sizes=(128, 256, 512), aspect_ratios=(0.5, 1.0, 2.0)):
        super(AnchorsGenerator, self).__init__()
        # 原默认参数生成3*3anchors，FPN作backbone每个特征图生成3anchors，共5*3anchors
        # sizes=((32,), (64,), (128,), (256,), (512,))
        # aspect_ratios=((0.5, 1.0, 2.0),) * len(sizes)
        if not isinstance(sizes[0], (list, tuple)):
            # TODO change this
            sizes = tuple((s,) for s in sizes)
        if not isinstance(aspect_ratios[0], (list, tuple)):
            aspect_ratios = (aspect_ratios,) * len(sizes)

        assert len(sizes) == len(aspect_ratios)

        self.sizes = sizes  # sizes=((32,), (64,), (128,), (256,), (512,))
        self.aspect_ratios = aspect_ratios  # aspect_ratios=((0.5, 1.0, 2.0),) * len(sizes)  list长度即为fpn输出特征图个数
        self.cell_anchors = None  # set_cell_anchors()：[5, 3, 4] 5个特征图，每点对应3个anchors
        self._cache = {}

    def generate_anchors(self, scales, aspect_ratios, dtype=torch.float32, device=torch.device("cpu")):
        # type: (List[int], List[float], torch.dtype, torch.device) -> Tensor
        """
        compute anchor sizes
        Arguments:
            scales: sqrt(anchor_area)
            aspect_ratios: h/w ratios
            dtype: float32
            device: cpu/gpu
        """
        # scales=(32,)...
        # aspect_ratios=(0.5, 1.0, 2.0)
        scales = torch.as_tensor(scales, dtype=dtype, device=device)
        aspect_ratios = torch.as_tensor(aspect_ratios, dtype=dtype, device=device)  # tensor([0.5000, 1.0000, 2.0000])
        h_ratios = torch.sqrt(aspect_ratios)  # 开根保证anchors面积相同，tensor([0.7071, 1.0000, 1.4142])
        w_ratios = 1.0 / h_ratios  # tensor([1.4142, 1.0000, 0.7071])


        # [r1, r2, r3]' * [s1, s2, s3]
        # number of elements is len(ratios)*len(scales)
        ws = (w_ratios[:, None] * scales[None, :]).view(-1)  # tensor([45.2548, 32.0000, 22.6274])
        hs = (h_ratios[:, None] * scales[None, :]).view(-1)  # tensor([22.6274, 32.0000, 45.2548])

        # left-top, right-bottom coordinate relative to anchor center(0, 0)
        # 生成的anchors模板都是以（0, 0）为中心的, shape [len(ratios)*len(scales), 4]
        base_anchors = torch.stack([-ws, -hs, ws, hs], dim=1) / 2  # [3,4]

        return base_anchors.round()  # round 四舍五入

    def set_cell_anchors(self, dtype, device):
        # type: (torch.dtype, torch.device) -> None
        if self.cell_anchors is not None:
            cell_anchors = self.cell_anchors
            assert cell_anchors is not None
            # suppose that all anchors have the same device
            # which is a valid assumption in the current state of the codebase
            if cell_anchors[0].device == device:
                return

        # 根据提供的sizes和aspect_ratios生成anchors模板
        # anchors模板都是以(0, 0)为中心的anchor
        cell_anchors = [
            self.generate_anchors(sizes, aspect_ratios, dtype, device)
            for sizes, aspect_ratios in zip(self.sizes, self.aspect_ratios)
        ]
        self.cell_anchors = cell_anchors  # [5, 3, 4] 5个特征图，每点对应3个anchors

    def num_anchors_per_location(self):
        # 计算每个预测特征层上每个滑动窗口的预测目标数k=3
        return [len(s) * len(a) for s, a in zip(self.sizes, self.aspect_ratios)]

    # For every combination of (a, (g, s), i) in (self.cell_anchors, zip(grid_sizes, strides), 0:2),
    # output g[i] anchors that are s[i] distance apart in direction i, with the same dimensions as a.
    def grid_anchors(self, grid_sizes, strides):
        # type: (List[List[int]], List[List[Tensor]]) -> List[Tensor]
        # grid_sizes: [5, 2]: [[feature_h, feature_w], ...]
        # strides: # [5, 2]: [[stride_h, stride_w], ...]
        """
        anchors position in grid coordinate axis map into origin image
        计算预测特征图对应原始图像上的所有anchors的坐标
        这里的原始图像是指GeneralizedRCNNTransform后的,即resized,padding后的
        Args:
            grid_sizes: 预测特征矩阵的height和width
            strides: 预测特征矩阵上一步对应原始图像上的步距
        """
        anchors = []
        cell_anchors = self.cell_anchors  # [5, 3, 4] 5个特征图，每点对应3
        assert cell_anchors is not None

        # 遍历每个预测特征层的grid_size，strides和cell_anchors
        for size, stride, base_anchors in zip(grid_sizes, strides, cell_anchors):
            grid_height, grid_width = size  # 特征图高、宽
            stride_height, stride_width = stride  # 原图步长
            device = base_anchors.device

            # For output anchor, compute [x_center, y_center, x_center, y_center]
            # shape: [grid_width] 对应原图上的x坐标(列)
            shifts_x = torch.arange(0, grid_width, dtype=torch.float32, device=device) * stride_width
            # shape: [grid_height] 对应原图上的y坐标(行)
            shifts_y = torch.arange(0, grid_height, dtype=torch.float32, device=device) * stride_height

            # 计算预测特征矩阵上每个点对应原图上的坐标(anchors模板的坐标偏移量)
            # torch.meshgrid函数分别传入行坐标和列坐标，生成网格行坐标矩阵和网格列坐标矩阵
            # shape: [grid_height, grid_width]
            shift_y, shift_x = torch.meshgrid(shifts_y, shifts_x)
            shift_x = shift_x.reshape(-1)
            shift_y = shift_y.reshape(-1)

            # 计算anchors坐标(xmin, ymin, xmax, ymax)在原图上的坐标偏移量
            # shape: [grid_width*grid_height, 4]
            shifts = torch.stack([shift_x, shift_y, shift_x, shift_y], dim=1)

            # For every (base anchor, output anchor) pair,
            # offset each zero-centered base anchor by the center of the output anchor.
            # 将anchors模板与原图上的坐标偏移量相加得到原图上所有anchors的坐标信息(shape不同时会使用广播机制)
            shifts_anchor = shifts.view(-1, 1, 4) + base_anchors.view(1, -1, 4)  # [grid_width*grid_height, 1, 4] + [1, 3, 4]
            anchors.append(shifts_anchor.reshape(-1, 4))

        # 每个特征图在padding图上生成了grid_width*grid_height*3的anchors
        return anchors  # List[Tensor(all_num_anchors, 4)] [5, all_num_anchors_i, 4]

    def cached_grid_anchors(self, grid_sizes, strides):
        # type: (List[List[int]], List[List[Tensor]]) -> List[Tensor]
        # grid_sizes: [5, 2]: [[feature_h, feature_w], ...]
        # strides: # [5, 2]: [[stride_h, stride_w], ...]
        """将计算得到的所有anchors信息进行缓存"""
        key = str(grid_sizes) + str(strides)
        # self._cache是字典类型
        if key in self._cache:
            return self._cache[key]
        anchors = self.grid_anchors(grid_sizes, strides)
        self._cache[key] = anchors
        return anchors

    def forward(self, image_list, feature_maps):
        # type: (ImageList, List[Tensor]) -> List[Tensor]
        # feature_maps: [batch_size, Ci, Hi, Wi]*5
        # 获取每个预测特征层的尺寸(height, width)
        grid_sizes = list([feature_map.shape[-2:] for feature_map in feature_maps])  # [5, 2]: [[f_h, f_w], ...]

        # 获取输入图像的height和width，resized和padding后的
        image_size = image_list.tensors.shape[-2:]

        # 获取变量类型和设备类型
        dtype, device = feature_maps[0].dtype, feature_maps[0].device

        # one step in feature map equate n pixel stride in origin image
        # 计算特征层上的一步等于原始图像上的步长
        strides = [[torch.tensor(image_size[0] // g[0], dtype=torch.int64, device=device),
                    torch.tensor(image_size[1] // g[1], dtype=torch.int64, device=device)] for g in grid_sizes]

        # 根据提供的sizes和aspect_ratios生成anchors模板
        self.set_cell_anchors(dtype, device)

        # 计算/读取所有anchors的坐标信息（这里的anchors信息是映射到原图上的所有anchors信息，不是anchors模板）
        # 得到的是一个list列表，对应每张预测特征图映射回原图的anchors坐标信息
        # [all_num_anchors_i, 4]*5
        anchors_over_all_feature_maps = self.cached_grid_anchors(grid_sizes, strides)

        # 存储一个batchsize中所有图片的所有anchors
        anchors = torch.jit.annotate(List[List[torch.Tensor]], [])
        # 遍历一个batch中的每张图像，同一batch中图像尺寸都一样，因此anchors数都一样
        for i, (image_height, image_width) in enumerate(image_list.image_sizes):
            anchors_in_image = []
            # 遍历每张预测特征图映射回原图的anchors坐标信息
            for anchors_per_feature_map in anchors_over_all_feature_maps:
                anchors_in_image.append(anchors_per_feature_map)
            anchors.append(anchors_in_image)
        # 将每一张图像的所有预测特征层的anchors坐标信息拼接在一起
        # anchors是个list，每个元素为一张图像的所有anchors信息
        # [[all_num_anchors_i, 4]*5]*batch_size -> [5*ANA_i, 4]*batch_size List[torch.Tensor] List[Tensor(all_num_anchors, 4)]
        anchors = [torch.cat(anchors_per_image) for anchors_per_image in anchors]
        # Clear the cache in case that memory leaks.
        self._cache.clear()
        return anchors  # [5*ANA_i, 4]*batch_size


class RPNHead(nn.Module):
    """
    add a RPN head with classification and regression
    通过滑动窗口计算预测目标概率与bbox regression参数

    Arguments:
        in_channels: number of channels of the input feature
        num_anchors: number of anchors to be predicted
    """

    def __init__(self, in_channels, num_anchors):
        super(RPNHead, self).__init__()
        # 3x3 滑动窗口
        self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1)
        # 计算预测的目标分数（这里的目标只是指前景或者背景）num_anchors=k=3
        self.cls_logits = nn.Conv2d(in_channels, num_anchors, kernel_size=1, stride=1)
        # 计算预测的目标bbox regression参数
        self.bbox_pred = nn.Conv2d(in_channels, num_anchors * 4, kernel_size=1, stride=1)

        for layer in self.children():
            if isinstance(layer, nn.Conv2d):
                torch.nn.init.normal_(layer.weight, std=0.01)
                torch.nn.init.constant_(layer.bias, 0)

    def forward(self, x):
        # type: (List[Tensor]) -> Tuple[List[Tensor], List[Tensor]]
        # input: [batch_size, Ci, Hi, Wi]*5
        logits = []
        bbox_reg = []
        for i, feature in enumerate(x):  # FPN x:{OrderedDict:5}
            t = F.relu(self.conv(feature))
            logits.append(self.cls_logits(t))
            bbox_reg.append(self.bbox_pred(t))
        return logits, bbox_reg


def permute_and_flatten(layer, N, A, C, H, W):
    # type: (Tensor, int, int, int, int, int) -> Tensor
    """
    调整tensor顺序，并进行reshape
    Args:
        layer: 预测特征层上预测的目标概率或bboxes regression参数
        N: batch_size
        A: anchors_num_per_position
        C: classes_num or 4(bbox coordinate)
        H: height
        W: width

    Returns:
        layer: 调整tensor顺序，并reshape后的结果[N, -1, C]
    """
    # view和reshape功能是一样的，先展平所有元素在按照给定shape排列
    # view函数只能用于内存中连续存储的tensor，permute等操作会使tensor在内存中变得不再连续，此时就不能再调用view函数
    # reshape则不需要依赖目标tensor是否在内存中是连续的
    # [batch_size, anchors_num_per_position * (C or 4), height, width]
    layer = layer.view(N, -1, C,  H, W)
    # 调换tensor维度
    layer = layer.permute(0, 3, 4, 1, 2)  # [N, H, W, -1, C]
    layer = layer.reshape(N, -1, C)
    return layer


def concat_box_prediction_layers(box_cls, box_regression):
    # type: (List[Tensor], List[Tensor]) -> Tuple[Tensor, Tensor]
    """
    [batch_size, 3, Hi, Wi]*5, [batch_size, 12, Hi, Wi]*5
    对box_cla和box_regression两个list中的每个预测特征层的预测信息
    的tensor排列顺序以及shape进行调整 -> [N, -1, C]
    Args:
        box_cls: 每个预测特征层上的预测目标概率
        box_regression: 每个预测特征层上的预测目标bboxes regression参数

    Returns:

    """
    box_cls_flattened = []
    box_regression_flattened = []

    # 遍历每个预测特征层
    for box_cls_per_level, box_regression_per_level in zip(box_cls, box_regression):
        # [batch_size, anchors_num_per_position * classes_num, height, width]
        # 注意，当计算RPN中的proposal时，classes_num=1,只区分目标和背景
        N, AxC, H, W = box_cls_per_level.shape  # [batch_size, 3, Hi, Wi]
        # [batch_size, anchors_num_per_position * 4, height, width]
        Ax4 = box_regression_per_level.shape[1]  # 3x4
        # anchors_num_per_position
        A = Ax4 // 4  # 3
        # classes_num
        C = AxC // A  # 1

        # [batch_size, 3x1, H, W] -> [batch_size, -1, 1]  -1:anchors_num_per_feature
        box_cls_per_level = permute_and_flatten(box_cls_per_level, N, A, C, H, W)
        box_cls_flattened.append(box_cls_per_level)

        # [batch_size, 3x4, H, W] -> [batch_size, -1, 4]
        box_regression_per_level = permute_and_flatten(box_regression_per_level, N, A, 4, H, W)
        box_regression_flattened.append(box_regression_per_level)

    # [batch_size, -1, 1]*5 -> [batch_size, 5*-1, 1] -> [batch_size*5*-1, 1]
    box_cls = torch.cat(box_cls_flattened, dim=1).flatten(0, -2)  # start_dim, end_dim
    box_regression = torch.cat(box_regression_flattened, dim=1).reshape(-1, 4)
    return box_cls, box_regression


class RegionProposalNetwork(torch.nn.Module):
    """
    Implements Region Proposal Network (RPN).

    Arguments:
        anchor_generator (AnchorGenerator): module that generates the anchors for a set of feature
            maps.
        head (nn.Module): module that computes the objectness and regression deltas
        fg_iou_thresh (float): minimum IoU between the anchor and the GT box so that they can be
            considered as positive during training of the RPN.
        bg_iou_thresh (float): maximum IoU between the anchor and the GT box so that they can be
            considered as negative during training of the RPN.
        batch_size_per_image (int): number of anchors that are sampled during training of the RPN
            for computing the loss
        positive_fraction (float): proportion of positive anchors in a mini-batch during training
            of the RPN
        pre_nms_top_n (Dict[str]): number of proposals to keep before applying NMS. It should
            contain two fields: training and testing, to allow for different values depending
            on training or evaluation
        post_nms_top_n (Dict[str]): number of proposals to keep after applying NMS. It should
            contain two fields: training and testing, to allow for different values depending
            on training or evaluation
        nms_thresh (float): NMS threshold used for postprocessing the RPN proposals

    """
    __annotations__ = {
        'box_coder': det_utils.BoxCoder,
        'proposal_matcher': det_utils.Matcher,
        'fg_bg_sampler': det_utils.BalancedPositiveNegativeSampler,
        'pre_nms_top_n': Dict[str, int],
        'post_nms_top_n': Dict[str, int],
    }

    def __init__(self, anchor_generator, head,
                 fg_iou_thresh, bg_iou_thresh,
                 batch_size_per_image, positive_fraction,
                 pre_nms_top_n, post_nms_top_n, nms_thresh, score_thresh=0.0):
        super(RegionProposalNetwork, self).__init__()
        self.anchor_generator = anchor_generator
        self.head = head  # RPNHead
        self.box_coder = det_utils.BoxCoder(weights=(1.0, 1.0, 1.0, 1.0))  # 计算边界框回归参数

        # use during training
        # 计算anchors与真实bbox的iou
        self.box_similarity = box_ops.box_iou

        self.proposal_matcher = det_utils.Matcher(
            fg_iou_thresh,  # 当iou大于fg_iou_thresh(0.7)时视为正样本
            bg_iou_thresh,  # 当iou小于bg_iou_thresh(0.3)时视为负样本
            allow_low_quality_matches=True
        )

        self.fg_bg_sampler = det_utils.BalancedPositiveNegativeSampler(
            batch_size_per_image, positive_fraction  # 256, 0.5 RPN在计算损失时采样正负样本总个数
        )

        # use during testing
        self._pre_nms_top_n = pre_nms_top_n  # {'training':2000,'testing':1000}
        self._post_nms_top_n = post_nms_top_n  # {'training':2000,'testing':1000}
        self.nms_thresh = nms_thresh  # 0.7
        self.score_thresh = score_thresh  # 0.0
        self.min_size = 1.

    def pre_nms_top_n(self):
        if self.training:
            return self._pre_nms_top_n['training']
        return self._pre_nms_top_n['testing']

    def post_nms_top_n(self):
        if self.training:
            return self._post_nms_top_n['training']
        return self._post_nms_top_n['testing']

    def assign_targets_to_anchors(self, anchors, targets):
        # type: (List[Tensor], List[Dict[str, Tensor]]) -> Tuple[List[Tensor], List[Tensor]]
        """
        计算每个anchors最匹配的gt，并划分为正样本，背景以及废弃的样本
        Args：
            anchors: (List[Tensor]) [5*ANA_i, 4]*batch_size batch
            targets: (List[Dict[Tensor])
        Returns:
            labels: 标记anchors归属类别（1, 0, -1分别对应正样本，背景，废弃的样本）
                    注意，在RPN中只有前景和背景，所有正样本的类别都是1，0代表背景
            matched_gt_boxes：与anchors匹配的gt
        """
        labels = []
        matched_gt_boxes = []
        # 遍历每张图像的anchors和targets
        for anchors_per_image, targets_per_image in zip(anchors, targets):
            gt_boxes = targets_per_image["boxes"]
            if gt_boxes.numel() == 0:
                device = anchors_per_image.device
                matched_gt_boxes_per_image = torch.zeros(anchors_per_image.shape, dtype=torch.float32, device=device)
                labels_per_image = torch.zeros((anchors_per_image.shape[0],), dtype=torch.float32, device=device)
            else:
                # 计算anchors与真实bbox的iou信息
                # set to self.box_similarity when https://github.com/pytorch/pytorch/issues/27495 lands
                # [一张图片GT_boxes个数，一张图片生成anchors数(fpn:20+w)]
                match_quality_matrix = box_ops.box_iou(gt_boxes, anchors_per_image)
                # 计算每个anchors与gt匹配iou最大的索引（如果iou<0.3索引置为-1，0.3<iou<0.7索引为-2）
                # [nums_all_anchors]  -1:负样本；-2:丢弃；>=0：GT索引（正样本）
                matched_idxs = self.proposal_matcher(match_quality_matrix)
                # get the targets corresponding GT for each proposal
                # NB: need to clamp the indices because we can have a single
                # GT in the image, and matched_idxs can be -2, which goes
                # out of bounds
                # 计算边界框回归损失时只使用索引>=0的正样本
                matched_gt_boxes_per_image = gt_boxes[matched_idxs.clamp(min=0)]

                labels_per_image = matched_idxs >= 0
                labels_per_image = labels_per_image.to(dtype=torch.float32)  # 1.0

                # background (negative examples)
                bg_indices = matched_idxs == self.proposal_matcher.BELOW_LOW_THRESHOLD  # -1
                labels_per_image[bg_indices] = 0.0

                # discard indices that are between thresholds
                inds_to_discard = matched_idxs == self.proposal_matcher.BETWEEN_THRESHOLDS  # -2
                labels_per_image[inds_to_discard] = -1.0

            labels.append(labels_per_image)
            matched_gt_boxes.append(matched_gt_boxes_per_image)
        return labels, matched_gt_boxes

    def _get_top_n_idx(self, objectness, num_anchors_per_level):
        # type: (Tensor, List[int]) -> Tensor
        """
        获取每张预测特征图上预测概率排前pre_nms_top_n的anchors索引值
        Args:
            objectness: Tensor(每张图像的预测目标概率信息 ) [batch_size, -1]
            num_anchors_per_level: List（每个预测特征层上的预测的anchors个数）[3*H_i*W_i]*5
        Returns:

        """
        r = []  # 记录每个预测特征层上预测目标概率前pre_nms_top_n的索引信息
        offset = 0
        # 遍历每个预测特征层上的预测目标概率信息
        for ob in objectness.split(num_anchors_per_level, 1):
            # ob: [batch_size, 3*H_i*W_i]
            if torchvision._is_tracing():
                num_anchors, pre_nms_top_n = _onnx_get_num_anchors_and_pre_nms_top_n(ob, self.pre_nms_top_n())
            else:
                num_anchors = ob.shape[1]  # 预测特征层上的预测的proposals个数
                pre_nms_top_n = min(self.pre_nms_top_n(), num_anchors)

            # Returns the k largest elements of the given input tensor along a given dimension
            _, top_n_idx = ob.topk(pre_nms_top_n, dim=1)  # 每张图都不一样
            r.append(top_n_idx + offset)
            offset += num_anchors
        return torch.cat(r, dim=1)  # [batch_size, <=2000 * 5]

    def filter_proposals(self, proposals, objectness, image_shapes, num_anchors_per_level):
        # type: (Tensor, Tensor, List[Tuple[int, int]], List[int]) -> Tuple[List[Tensor], List[Tensor]]
        """
        1.根据预测概率获取前pre\post_nms_top_n个目标(每个特征图对应的)
        2.筛除小boxes框
        3.nms处理
        Args:
            proposals: 所有图片预测的bbox坐标  [num_anchors, 4]*batch_size
            objectness: 所有图片预测的目标概率  [-1,1]
            image_shapes: batch中每张图片的size信息resized后无padding  [(h1,w1),(h2,w2),...]
            num_anchors_per_level: 一张图片每个预测特征层上预测anchors的数目  [3*H_i*W_i]*5

        Returns:

        """
        num_images = proposals.shape[0]  # batch_size
        device = proposals.device

        # do not backprop throught objectness
        objectness = objectness.detach()
        objectness = objectness.reshape(num_images, -1)  # [batch_size, -1]

        # Returns a tensor of size size filled with fill_value
        # levels负责记录分隔不同预测特征层上的proposals索引信息
        # 便于后面只对相同特征图上生成proposals进行nms
        # 记录proposals中每个anchor属于哪个特征层
        levels = [torch.full((n, ), idx, dtype=torch.int64, device=device)
                  for idx, n in enumerate(num_anchors_per_level)]  # [[0,0,...],[1,1,...],...]
        levels = torch.cat(levels, 0)  # [0,0,0,...,1,1,...]

        # Expand this tensor to the same size as objectness
        # 复制batch_size份，同batch中每副图像各个特征图对应生成的anchors数都一样
        levels = levels.reshape(1, -1).expand_as(objectness)  # [batch_size, -1]

        # select top_n boxes independently per level before applying nms
        # 获取每张预测特征图上预测概率排前pre_nms_top_n的proposals索引值
        # [batch_size, <=2000 * 5]
        top_n_idx = self._get_top_n_idx(objectness, num_anchors_per_level)

        image_range = torch.arange(num_images, device=device)
        batch_idx = image_range[:, None]  # [batch_size, 1]

        # 根据每个预测特征层预测概率排前pre_nms_top_n的proposals索引值获取相应概率信息
        objectness = objectness[batch_idx, top_n_idx]  # [batch_size, -1]
        levels = levels[batch_idx, top_n_idx]  # [batch_size, -1]
        # 预测概率排前pre_nms_top_n的proposals索引值获取相应bbox坐标信息
        proposals = proposals[batch_idx, top_n_idx]  # [batch_size, -1, 4]

        objectness_prob = torch.sigmoid(objectness)

        final_boxes = []
        final_scores = []
        # 遍历每张图像的相关预测信息
        for boxes, scores, lvl, img_shape in zip(proposals, objectness_prob, levels, image_shapes):
            # 调整预测的boxes信息，将越界的坐标调整到图片边界上
            boxes = box_ops.clip_boxes_to_image(boxes, img_shape)

            # 返回boxes满足宽，高都大于min_size的索引
            keep = box_ops.remove_small_boxes(boxes, self.min_size)
            boxes, scores, lvl = boxes[keep], scores[keep], lvl[keep]

            # 移除小概率boxes，参考下面这个链接
            # self.score_thresh默认为0.0，经过sigmoid后，scores都满足
            # https://github.com/pytorch/vision/pull/3205
            # predict 时设置为0.5将滤除掉大量背景， train时设置为0保留背景，以便roi_head有负样本进行训练
            keep = torch.where(torch.ge(scores, self.score_thresh))[0]  # ge: >=
            boxes, scores, lvl = boxes[keep], scores[keep], lvl[keep]

            # non-maximum suppression, independently done per level
            # self.nms_thresh = 0.7
            keep = box_ops.batched_nms(boxes, scores, lvl, self.nms_thresh)

            # keep only topk scoring predictions
            # 前面是每个特征图最多保留self.pre_nms_top_n()个目标，这里是总共保留目标数
            keep = keep[: self.post_nms_top_n()]
            boxes, scores = boxes[keep], scores[keep]

            final_boxes.append(boxes)
            final_scores.append(scores)
        return final_boxes, final_scores

    def compute_loss(self, objectness, pred_bbox_deltas, labels, regression_targets):
        # type: (Tensor, Tensor, List[Tensor], List[Tensor]) -> Tuple[Tensor, Tensor]
        """
        计算RPN损失，包括类别损失（前景与背景），bbox regression损失
        Arguments:
            objectness (Tensor)：预测的前景概率
            pred_bbox_deltas (Tensor)：预测的bbox regression
            labels (List[Tensor])：真实的标签 1, 0, -1（batch中每一张图片的labels对应List的一个元素中）
            regression_targets (List[Tensor])：真实的bbox regression

        Returns:
            objectness_loss (Tensor) : 类别损失
            box_loss (Tensor)：边界框回归损失
        """
        # 按照给定的batch_size_per_image, positive_fraction选择正负样本
        sampled_pos_inds, sampled_neg_inds = self.fg_bg_sampler(labels)
        # 将一个batch中的所有正负样本List(Tensor)分别拼接在一起，并获取非零位置的索引
        # sampled_pos_inds = torch.nonzero(torch.cat(sampled_pos_inds, dim=0)).squeeze(1)
        sampled_pos_inds = torch.where(torch.cat(sampled_pos_inds, dim=0))[0]
        # sampled_neg_inds = torch.nonzero(torch.cat(sampled_neg_inds, dim=0)).squeeze(1)
        sampled_neg_inds = torch.where(torch.cat(sampled_neg_inds, dim=0))[0]

        # 将所有正负样本索引拼接在一起
        sampled_inds = torch.cat([sampled_pos_inds, sampled_neg_inds], dim=0)  # [<=256*batch_size,1]
        objectness = objectness.flatten()  # [-1,1]

        labels = torch.cat(labels, dim=0)  # [-1,1]
        regression_targets = torch.cat(regression_targets, dim=0)  # [-1,1]

        # 计算边界框回归损失(只计算正样本)
        box_loss = det_utils.smooth_l1_loss(
            pred_bbox_deltas[sampled_pos_inds],
            regression_targets[sampled_pos_inds],
            beta=1 / 9,
            size_average=False,
        ) / (sampled_inds.numel())  # 并没有除以正样本数

        # 计算目标预测概率损失(样本不平衡)
        # objectness_loss = F.binary_cross_entropy_with_logits(
        #     objectness[sampled_inds], labels[sampled_inds]
        # )
        alpha = len(sampled_neg_inds) / (len(sampled_pos_inds) + len(sampled_neg_inds))
        gamma = 1  # gamma=0 即原始交叉熵
        objectness_loss = torchvision.ops.sigmoid_focal_loss(
            objectness[sampled_inds], labels[sampled_inds], alpha, gamma, reduction='mean'
        )

        return objectness_loss, box_loss

    def forward(self,
                images,        # type: ImageList
                features,      # type: Dict[str, Tensor]
                targets=None   # type: Optional[List[Dict[str, Tensor]]]
                ):
        # type: (...) -> Tuple[List[Tensor], Dict[str, Tensor]]
        """
        Arguments:
            images (ImageList): images for which we want to compute the predictions
            features (Dict[Tensor]): features computed from the images that are
                used for computing the predictions. Each tensor in the list
                correspond to different feature levels
            targets (List[Dict[Tensor]): ground-truth boxes present in the image (optional).
                If provided, each element in the dict should contain a field `boxes`,
                with the locations of the ground-truth boxes.

        Returns:
            boxes (List[Tensor]): the predicted boxes from the RPN, one Tensor per
                image.
            losses (Dict[Tensor]): the losses for the model during training. During
                testing, it is an empty dict.
        """
        # RPN uses all feature maps that are available
        # features是所有预测特征层组成的OrderedDict
        # resnet50-fpn：features：｛'0':f1,'1':f2,'2':f3,'3':f4,'pool':f5｝
        # f1.shape=[batch_size, 256, H, W]
        features = list(features.values())

        # 计算每个预测特征层上的预测目标概率和bboxes regression参数
        # objectness和pred_bbox_deltas都是list [batch_size, 3, Hi, Wi]*5 [ batch_size, 12, Hi, Wi]*5
        objectness, pred_bbox_deltas = self.head(features)

        # 生成一个batch图像的所有anchors信息,list(tensor)元素个数等于batch_size
        # [5*ANA_i, 4]*batch_size batch中每副图像大概有上万多个anchors（fpn:21W）
        anchors = self.anchor_generator(images, features)

        # batch_size
        num_images = len(anchors)

        # numel() Returns the total number of elements in the input tensor.
        # 计算每个预测特征层上的对应的anchors数量
        num_anchors_per_level_shape_tensors = [o[0].shape for o in objectness]
        num_anchors_per_level = [s[0] * s[1] * s[2] for s in num_anchors_per_level_shape_tensors]  # [3*H_i*W_i]*5

        # 调整内部tensor格式以及shape, objectness:[-1,1] pred_bbox_deltas:[-1,4] -1:一个batch中所有图片对应的所有特征图对应的anchors的cls和bbox_deltas输出
        objectness, pred_bbox_deltas = concat_box_prediction_layers(objectness,
                                                                    pred_bbox_deltas)

        # 这里开始对objectness, pred_bbox_deltas截流detach()
        # apply pred_bbox_deltas to anchors to obtain the decoded proposals
        # note that we detach the deltas because Faster R-CNN do not backprop through
        # the proposals 就是后面更新ROIHead参数时RPN和Backbone是冻结的？
        # 将预测的bbox regression参数应用到anchors上得到最终预测bbox坐标
        proposals = self.box_coder.decode(pred_bbox_deltas.detach(), anchors)
        proposals = proposals.view(num_images, -1, 4)  # [batch_size, num_anchors, 4]

        # 筛除小boxes框，nms处理，根据预测概率获取前post_nms_top_n个目标
        boxes, scores = self.filter_proposals(proposals, objectness, images.image_sizes, num_anchors_per_level)

        losses = {}
        if self.training:
            assert targets is not None
            # 计算每个anchors最匹配的gt，并将anchors进行分类，前景，背景以及废弃的anchors
            # labels: -1.0:丢弃、0.0:负样本、1.0：正样本 [nums_all_anchors]*batch_size
            # matched_gt_boxes: [nums_all_anchors, 4]*batch_size
            labels, matched_gt_boxes = self.assign_targets_to_anchors(anchors, targets)
            # 结合anchors以及对应的gt，计算regression参数
            regression_targets = self.box_coder.encode(matched_gt_boxes, anchors)  # [-1, 4]*batch_size
            loss_objectness, loss_rpn_box_reg = self.compute_loss(
                objectness, pred_bbox_deltas, labels, regression_targets
            )
            losses = {
                "loss_objectness": loss_objectness,
                "loss_rpn_box_reg": loss_rpn_box_reg
            }
        return boxes, losses

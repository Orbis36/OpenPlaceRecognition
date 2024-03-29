from pyexpat import model
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.neighbors import NearestNeighbors
from .feature_select_template import FeatureSelectTemplate
import numpy as np

# based on https://github.com/lyakaap/NetVLAD-pytorch/blob/master/netvlad.py
class NetVLAD(FeatureSelectTemplate):
    """NetVLAD layer implementation"""
    def __init__(self, model_cfg):
        super().__init__(model_cfg=model_cfg)
        """
        Args:
            num_clusters : int
                The number of clusters
            dim : int
                Dimension of descriptors
            alpha : float
                Parameter of initialization. Larger value is harder assignment.
            normalize_input : bool
                If true, descriptor-wise L2 normalization is applied to input.
            vladv2 : bool
                If true, use vladv2 otherwise use vladv1
        """
        self.num_clusters = model_cfg.CLUSTERS_NUM
        self.alpha = 0
        self.vladv2 = model_cfg.VLAD_NEW_VERSION
        self.normalize_input = model_cfg.NORMALIZE
        self.conv = nn.Conv2d(model_cfg.BACKBONE_OUT_DIM, self.num_clusters, kernel_size=(1, 1), bias=self.vladv2)
        self.centroids = nn.Parameter(torch.rand(self.num_clusters, model_cfg.BACKBONE_OUT_DIM))

    def init_params(self, clsts, traindescs):
        print("New Vlad Using {}".format(self.vladv2))
        if self.vladv2 == False:
            clstsAssign = clsts / np.linalg.norm(clsts, axis=1, keepdims=True)
            dots = np.dot(clstsAssign, traindescs.T)
            dots.sort(0)
            dots = dots[::-1, :] # sort, descending

            self.alpha = (-np.log(0.01) / np.mean(dots[0,:] - dots[1,:])).item()
            self.centroids = nn.Parameter(torch.from_numpy(clsts))
            self.conv.weight = nn.Parameter(torch.from_numpy(self.alpha*clstsAssign).unsqueeze(2).unsqueeze(3))
            self.conv.bias = None
        else:
            knn = NearestNeighbors(n_jobs=-1) #TODO faiss?
            knn.fit(traindescs)
            del traindescs
            dsSq = np.square(knn.kneighbors(clsts, 2)[1])
            del knn
            self.alpha = (-np.log(0.01) / np.mean(dsSq[:,1] - dsSq[:,0])).item()
            self.centroids = nn.Parameter(torch.from_numpy(clsts))
            del clsts, dsSq

            self.conv.weight = nn.Parameter(
                (2.0 * self.alpha * self.centroids).unsqueeze(-1).unsqueeze(-1)
            )
            self.conv.bias = nn.Parameter(
                - self.alpha * self.centroids.norm(dim=1)
            )
        print("VLAD layer initialized")

    def forward(self, data_dict):
        x = data_dict['feature_map']
        N, C = x.shape[:2]

        if self.normalize_input:
            x = F.normalize(x, p=2, dim=1)  # across descriptor dim

        # soft-assignment
        soft_assign = self.conv(x).view(N, self.num_clusters, -1)
        soft_assign = F.softmax(soft_assign, dim=1)

        x_flatten = x.view(N, C, -1)
        
        # calculate residuals to each clusters
        vlad = torch.zeros([N, self.num_clusters, C], dtype=x.dtype, layout=x.layout, device=x.device)
        for C in range(self.num_clusters): # slower than non-looped, but lower memory usage 
            residual = x_flatten.unsqueeze(0).permute(1, 0, 2, 3) - \
                    self.centroids[C:C+1, :].expand(x_flatten.size(-1), -1, -1).permute(1, 2, 0).unsqueeze(0)
            a = soft_assign[:,C:C+1,:].unsqueeze(2)
            residual *= soft_assign[:,C:C+1,:].unsqueeze(2)
            b = residual.sum(dim=-1)
            vlad[:,C:C+1,:] = residual.sum(dim=-1)

        vlad = F.normalize(vlad, p=2, dim=2)  # intra-normalization
        vlad = vlad.view(x.size(0), -1)  # flatten
        vlad = F.normalize(vlad, p=2, dim=1)  # L2 normalize
        data_dict['clustered_feature'] = vlad
        return data_dict

    def get_feature_selected_loss(self, data_dict):
        
        features_encoded = data_dict['clustered_feature']
        B = data_dict['negUse'].shape[0]
        nNeg = torch.sum(data_dict['negUse'])
        # negCounts 是各个batch中负样本数目
        negCounts = data_dict['negUse']

        vladQ, vladP, vladN = torch.split(features_encoded, [B, B, nNeg])
        loss = 0
        for i, negCount in enumerate(negCounts):
            for n in range(negCount):
                negIx = (torch.sum(negCounts[:i]) + n).item()
                loss += self.triplet_loss_func(vladQ[i: i + 1], vladP[i: i + 1], vladN[negIx:negIx + 1])

        loss /= nNeg # normalise by actual number of negatives
        return loss

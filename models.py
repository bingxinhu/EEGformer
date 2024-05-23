import os
import torch
import torch.nn as nn
import math
import torch.nn.functional as F
import torchvision.ops as tos


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def trunc_normal(tensor, mean=0., std=1., a=-2., b=2.):  # for positional embedding - borrowed from meta
    def norm_cdf(x):  # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        print("mean is more than 2 std from [a, b] in nn.init.trunc_normal\nThe distribution of values may be incorrect.")

    with torch.no_grad(): # Values are generated by using a truncated uniform distribution and then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


class ODCM(nn.Module):
    def __init__(self, input_channels, kernel_size, dtype=torch.float32):
        super(ODCM, self).__init__()
        self.inpch = input_channels
        self.ksize = kernel_size  # 1X10
        self.ncf = 120  # The number of the depth-wise convolutional filter used in the three layers is set to 120
        self.dtype = dtype

        self.cvf1 = nn.Conv1d(in_channels=self.inpch, out_channels=self.inpch, kernel_size=self.ksize, padding='valid', stride=1, groups=self.inpch, dtype=self.dtype)
        self.cvf2 = nn.Conv1d(in_channels=self.cvf1.out_channels, out_channels=self.cvf1.out_channels, kernel_size=self.ksize, padding='valid', stride=1, groups=self.cvf1.out_channels, dtype=self.dtype)
        self.cvf3 = nn.Conv1d(in_channels=self.cvf2.out_channels, out_channels=self.ncf * self.cvf2.out_channels, kernel_size=self.ksize, padding='valid', stride=1, groups=self.cvf2.out_channels, dtype=self.dtype)

        # - reserve
        # self.relu = nn.ReLU()

    def forward(self, x):
        x = self.cvf1(x)
        # x = self.relu(x)

        x = self.cvf2(x)
        # x = self.relu(x)

        x = self.cvf3(x)
        x = torch.reshape(x, ((int)(x.shape[0] / self.ncf), self.ncf, (int)(x.shape[1])))

        return x


class Mlp(nn.Module):  # MLP from torchvision did not support float16 - borrowed from Meta
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0., dtype=torch.float32):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features, dtype=dtype)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features, dtype=dtype)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class RTM(nn.Module):  # Regional transformer module
    def __init__(self, input, num_blocks, num_heads, dtype):  # input -> S x C x D
        super(RTM, self).__init__()
        self.inputshape = input.transpose(0, 1).transpose(1, 2).shape  # C x D x S
        self.M_size1 = self.inputshape[1]  # -> D
        self.dtype = dtype

        self.tK = num_blocks  # number of transformer blocks - K in the paper
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)  # Dh is the quotient computed by D/A and denotes the dimension number of three vectors.

        if self.M_size1 % self.hA != 0 or int(self.M_size1 / self.hA) == 0:
            print(f"ERROR 1 - RTM : self.Dh = {int(self.M_size1 / self.hA)} != {self.M_size1}/{self.hA} \nTry with different num_heads")

        self.weight = nn.Parameter(torch.randn(self.M_size1, self.inputshape[1], dtype=self.dtype))
        self.bias = nn.Parameter(torch.zeros(self.inputshape[2], self.inputshape[0] + 1, self.M_size1, dtype=self.dtype))  # S x C x D
        self.cls = nn.Parameter(torch.zeros(self.inputshape[2], 1, self.M_size1, dtype=self.dtype))
        trunc_normal(self.bias, std=.02)
        trunc_normal(self.cls, std=.02)

        self.Wqkv = nn.Parameter(torch.randn((self.tK, 3, self.hA, self.Dh, self.M_size1), dtype=self.dtype))
        self.Wo = nn.Parameter(torch.randn(self.tK, self.M_size1, self.M_size1, dtype=self.dtype))
        self.lnorm = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for dimension D
        self.lnormz = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for z
        self.softmax = nn.Softmax()
        self.mlp = Mlp(in_features=self.M_size1, hidden_features=int(self.M_size1 * 4), act_layer=nn.GELU, dtype=self.dtype)  # mlp_ratio=4

    def forward(self, x):
        x = x.transpose(0, 1).transpose(1, 2)  # C x D x S

        savespace = torch.zeros(x.shape[2], x.shape[0], self.M_size1, dtype=self.dtype).to(device)  # S x C x D
        savespace = torch.einsum('lm,jmi -> ijl', self.weight, x)
        savespace = torch.cat((self.cls, savespace), dim=1)  # ! -> S x (C+1) x D
        savespace = torch.add(savespace, self.bias)  # z -> S x C x D

        qkvspace = torch.zeros(self.tK, 3, x.shape[2], x.shape[0] + 1, self.hA, self.Dh, dtype=self.dtype).to(device)  # Q, K, V
        rsaspace = torch.zeros(self.tK, x.shape[2], x.shape[0] + 1, self.hA, dtype=self.dtype).to(device)
        imv = torch.zeros(self.tK, x.shape[2], x.shape[0] + 1, self.hA, self.Dh, dtype=self.dtype).to(device)

        # TODO : generic blocks for parameter separation
        for a in range(self.tK):  # blocks(layer)
            qkvspace[a] = torch.einsum('xhdm,ijm -> xijhd', self.Wqkv[a], self.lnorm(savespace))  # Q, K, V

            # - Attention score
            rsaspace[a] = torch.einsum('ijhd,ijhd -> ijh', qkvspace[a, 0].clone() / math.sqrt(self.Dh), qkvspace[a, 1].clone())

            # - Intermediate vectors
            imv[a] = torch.einsum('ijh,ijhd -> ijhd', rsaspace[a].clone(), qkvspace[a, 2].clone())

            # TODO : fix interpretation of sigma
            for subj in range(1, self.inputshape[0]):
                imv[a, :, subj] = imv[a, :, subj] + imv[a, :, subj - 1]

            # - NOW SAY HELLO TO NEW Z!
            savespace = torch.einsum('nm,ijm -> ijn', self.Wo[a], imv.clone().reshape(self.tK, x.shape[2], x.shape[0] + 1, self.M_size1)[a]) + savespace  # z'

            # - normalized by LN() and passed through a multilayer perceptron (MLP)
            savespace = self.mlp(self.lnormz(savespace)) + savespace  # new z

        return savespace  # S x C x D - z4 in the paper


class STM(nn.Module):  # Synchronous transformer module
    def __init__(self, input, num_blocks, num_heads, dtype):  # input -> # S x C x D
        super(STM, self).__init__()
        self.inputshape = input.transpose(1, 2).shape  # S x D x C (S x Le x C in the paper)
        self.M_size1 = self.inputshape[1]  # -> D
        self.dtype = dtype

        self.tK = num_blocks  # number of transformer blocks - K in the paper
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)  # Dh is the quotient computed by D/A and denotes the dimension number of three vectors.

        if self.M_size1 % self.hA != 0 or int(self.M_size1 / self.hA) == 0:
            print(f"ERROR 2 - STM : self.Dh = {int(self.M_size1 / self.hA)} != {self.M_size1}/{self.hA} \nTry with different num_heads")

        self.weight = nn.Parameter(torch.randn(self.M_size1, self.inputshape[1], dtype=self.dtype))
        self.bias = nn.Parameter(torch.zeros(self.inputshape[2], self.inputshape[0] + 1, self.M_size1, dtype=self.dtype))  # S x C x D
        self.cls = nn.Parameter(torch.zeros(self.inputshape[2], 1, self.M_size1, dtype=self.dtype))
        trunc_normal(self.bias, std=.02)
        trunc_normal(self.cls, std=.02)

        self.Wqkv = nn.Parameter(torch.randn(self.tK, 3, self.hA, self.Dh, self.M_size1, dtype=self.dtype))
        self.Wo = nn.Parameter(torch.randn(self.tK, self.M_size1, self.M_size1, dtype=self.dtype))  # DxDh in the paper but we gonna concatenate heads anyway and Dh*hA = D
        self.lnorm = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for dimension D
        self.lnormz = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for z
        self.softmax = nn.Softmax()
        self.mlp = Mlp(in_features=self.M_size1, hidden_features=int(self.M_size1 * 4), act_layer=nn.GELU, dtype=self.dtype)  # mlp_ratio=4

    def forward(self, x): # S x C x D -> x
        x = x.transpose(1, 2)  # S x D x C

        savespace = torch.zeros(x.shape[2], x.shape[0] + 1, self.M_size1, dtype=self.dtype).to(device)  # C x S x D
        savespace = torch.einsum('lm,jmi -> ijl', self.weight, x)
        savespace = torch.cat((self.cls, savespace), dim=1)  # ! -> from C+1 x S x D to C+1 x S+1 x D
        savespace = torch.add(savespace, self.bias)  # z -> C x S x D

        qkvspace = torch.zeros(self.tK, 3, x.shape[2], x.shape[0] + 1, self.hA, self.Dh, dtype=self.dtype).to(device)  # Q, K, V
        rsaspace = torch.zeros(self.tK, x.shape[2], x.shape[0] + 1, self.hA, dtype=self.dtype).to(device)
        imv = torch.zeros(self.tK, x.shape[2], x.shape[0] + 1, self.hA, self.Dh, dtype=self.dtype).to(device)

        for a in range(self.tK):  # blocks(layer)
            qkvspace[a] = torch.einsum('xhdm,ijm -> xijhd', self.Wqkv[a], self.lnorm(savespace))  # Q, K, V

            # - Attention score
            rsaspace[a] = torch.einsum('ijhd,ijhd -> ijh', qkvspace[a, 0].clone() / math.sqrt(self.Dh), qkvspace[a, 1].clone())

            # - Intermediate vectors
            imv[a] = torch.einsum('ijh,ijhd -> ijhd', rsaspace[a].clone(), qkvspace[a, 2].clone())

            for subj in range(1, x.shape[0]):
                imv[a, :, subj] = imv[a, :, subj] + imv[a, :, subj - 1]

            # - NOW SAY HELLO TO NEW Z!
            savespace = torch.einsum('nm,ijm -> ijn', self.Wo[a], imv.clone().reshape(self.tK, x.shape[2], x.shape[0] + 1, self.M_size1)[a]) + savespace  # z'

            # - normalized by LN() and passed through a multilayer perceptron (MLP)
            savespace = self.mlp(self.lnormz(savespace)) + savespace  # new z

        return savespace  # C x S x D - z5 in the paper


class TTM(nn.Module):  # Temporal transformer module
    def __init__(self, input, num_submatrices, num_blocks, num_heads, dtype):  # input -> # C x S x D
        super(TTM, self).__init__()
        self.dtype = dtype
        self.avgf = num_submatrices  # average factor (M)
        self.input = input.transpose(0, 2)  # D x S x C
        self.seg = self.input.shape[0] / self.avgf

        if self.input.shape[0] % self.avgf != 0 or int(self.input.shape[0] / self.avgf) == 0:
            print(f"ERROR 3 - TTM : self.seg = {self.seg} != {self.input.shape[0]}/{self.avgf}")

        self.M_size1 = self.input.shape[1] * self.input.shape[2]
        self.tK = num_blocks  # number of transformer blocks - K in the paper
        self.hA = num_heads  # number of multi-head self-attention units (A is the number of units in a block)
        self.Dh = int(self.M_size1 / self.hA)

        if self.M_size1 % self.hA != 0 or int(self.M_size1 / self.hA) == 0:  # - Dh = 121*(S+1) / num_heads
            print(f"ERROR 4 - TTM : self.Dh = {int(self.M_size1 / self.hA)} != {self.M_size1}/{self.hA} \nTry with different num_heads")

        self.weight = nn.Parameter(torch.randn(self.M_size1, self.input.shape[1] * self.input.shape[2], dtype=self.dtype))
        self.bias = nn.Parameter(torch.zeros(self.avgf + 1, self.M_size1, dtype=self.dtype))
        self.cls = nn.Parameter(torch.zeros(1, self.M_size1, dtype=self.dtype))
        trunc_normal(self.bias, std=.02)
        trunc_normal(self.cls, std=.02)

        self.Wqkv = nn.Parameter(torch.randn(self.tK, 3, self.hA, self.Dh, self.M_size1, dtype=self.dtype))
        self.Wo = nn.Parameter(torch.randn(self.tK, self.M_size1, self.M_size1, dtype=self.dtype))
        self.lnorm = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for dimension D
        self.lnormz = nn.LayerNorm(self.M_size1, dtype=self.dtype)  # LayerNorm operation for z
        self.softmax = nn.Softmax()
        self.mlp = Mlp(in_features=self.M_size1, hidden_features=int(self.M_size1 * 4), act_layer=nn.GELU, dtype=self.dtype)  # mlp_ratio=4

        self.lnorm_extra = nn.LayerNorm(self.M_size1, dtype=self.dtype) # EXPERIMENTAL

    def forward(self, x):
        input = x.transpose(0, 2)  # D x S x C
        inputc = torch.zeros(self.avgf, input.shape[1], input.shape[2], dtype=self.dtype).to(device)  # M x S x C
        for i in range(0, self.avgf):  # each i consists self.input.shape[0]/avgf
            for j in range(int(i * self.seg), int((i + 1) * self.seg)):  # int(i*self.seg), int((i+1)*self.seg)
                inputc[i, :, :] = inputc[i, :, :] + input[j, :, :]
            inputc[i, :, :] = inputc[i, :, :] / self.seg

        altx = inputc.reshape(self.avgf, input.shape[1] * input.shape[2]).to(device)  # M x L -> M x (S*C)

        savespace = torch.zeros(self.avgf, self.M_size1, dtype=self.dtype).to(device)  # M x D
        savespace = torch.einsum('lm,im -> il', self.weight, altx.clone())
        savespace = torch.cat((self.cls, savespace), dim=0)
        savespace = torch.add(savespace, self.bias)  # z -> M x D

        qkvspace = torch.zeros(self.tK, 3, self.avgf + 1, self.hA, self.Dh, dtype=self.dtype).to(device)  # Q, K, V
        rsaspace = torch.zeros(self.tK, self.avgf + 1, self.hA, dtype=self.dtype).to(device)  # space for attention score
        imv = torch.zeros(self.tK, self.avgf + 1, self.hA, self.Dh, dtype=self.dtype).to(device)

        for a in range(self.tK):  # blocks(layer)
            qkvspace[a] = torch.einsum('xhdm,im -> xihd', self.Wqkv[a], self.lnorm(savespace))  # Q, K, V

            # - Attention score
            rsaspace[a] = torch.einsum('ihd,ihd -> ih', qkvspace[a, 0].clone() / math.sqrt(self.Dh), qkvspace[a, 1].clone())

            # - Intermediate vectors
            imv[a] = torch.einsum('ih,ihd -> ihd', rsaspace[a].clone(), qkvspace[a, 2].clone())

            for subj in range(1, self.avgf):
                imv[a, subj] = imv[a, subj] + imv[a, subj - 1]

            # - NOW SAY HELLO TO NEW Z!
            savespace = torch.einsum('nm,im -> in', self.Wo[a], imv.clone().reshape(self.tK, self.avgf + 1, self.M_size1)[a]) + savespace  # z'

            # - normalized by LN() and passed through a multilayer perceptron (MLP)
            savespace = self.mlp(self.lnormz(savespace)) + savespace # new z

        savespace = self.lnorm_extra(savespace) # EXPERIMENTAL
        return savespace.reshape(self.avgf + 1, input.shape[1], input.shape[2])


class CNNdecoder(nn.Module):  # EEGformer decoder
    def __init__(self, input, num_cls, CF_second, dtype):  # input -> # M x S x C
        super(CNNdecoder, self).__init__()
        self.input = input.transpose(0, 1).transpose(1, 2)  # S x C x M
        self.s = self.input.shape[0]  # S
        self.c = self.input.shape[1]  # C
        self.m = self.input.shape[2]  # M
        self.n = CF_second
        self.dtype = dtype
        self.cvd1 = nn.Conv1d(in_channels=self.c, out_channels=1, kernel_size=1, dtype=self.dtype)  # S x M
        self.cvd2 = nn.Conv1d(in_channels=self.s, out_channels=self.n, kernel_size=1, dtype=self.dtype)
        self.cvd3 = nn.Conv1d(in_channels=self.m, out_channels=int(self.m / 2), kernel_size=1, dtype=self.dtype)
        self.fc = nn.Linear(int(self.m / 2) * self.n, num_cls, dtype=self.dtype)

    def forward(self, x):  # x -> M x S x C
        x = x.transpose(0, 1).transpose(1, 2)  # S x C x M
        x = self.cvd1(x)  # S x M
        x = x[:, 0, :]
        x = self.cvd2(x).transpose(0, 1)  # N x M transposed to M x N
        x = self.cvd3(x)  # M/2 x N
        x = self.fc(x.reshape(1, x.shape[0] * x.shape[1]))

        return x


class EEGformer(nn.Module):
    def __init__(self, input, num_cls, input_channels, kernel_size, num_blocks, num_heads_RTM, num_heads_STM, num_heads_TTM, num_submatrices, CF_second, dtype=torch.float32):
        super(EEGformer, self).__init__()
        self.dtype = dtype
        self.ncf = 120
        self.num_cls = num_cls
        self.input_channels = input_channels
        self.kernel_size = kernel_size
        self.tK = num_blocks
        self.hA_rtm = num_heads_RTM
        self.hA_stm = num_heads_STM
        self.hA_ttm = num_heads_TTM
        self.avgf = num_submatrices
        self.cfs = CF_second

        self.outshape1 = torch.zeros(self.input_channels, self.ncf, input.shape[0] - 3 * (self.kernel_size - 1)).to(device)
        self.outshape2 = torch.zeros(self.outshape1.shape[0], self.outshape1.shape[1] + 1, self.outshape1.shape[2]).to(device)
        self.outshape3 = torch.zeros(self.outshape2.shape[1], self.outshape2.shape[0] + 1, self.outshape2.shape[2]).to(device)
        self.outshape4 = torch.zeros(self.avgf+1, self.outshape3.shape[1], self.outshape3.shape[0]).to(device)

        self.odcm = ODCM(input_channels, self.kernel_size, self.dtype)
        self.rtm = RTM(self.outshape1, self.tK, self.hA_rtm, self.dtype)
        self.stm = STM(self.outshape2, self.tK, self.hA_stm, self.dtype)
        self.ttm = TTM(self.outshape3, self.avgf, self.tK, self.hA_ttm, self.dtype)
        self.cnndecoder = CNNdecoder(self.outshape4, self.num_cls, self.cfs, self.dtype)

    def forward(self, x):
        x = self.odcm(x.transpose(0, 1))
        x = self.rtm(x)
        x = self.stm(x)
        x = self.ttm(x)
        x = self.cnndecoder(x)

        return torch.softmax(x, dim=1)


    # CE - uses one hot encoded label or similar(such as multi class probability label)
    def eegloss(self, xf, label, L1_reg_const):  # CE Loss with L1 regularization
        wt = self.sa(self.cnndecoder.fc.weight) + self.sa(self.cnndecoder.cvd1.weight) + self.sa(self.cnndecoder.cvd2.weight) + self.sa(self.cnndecoder.cvd3.weight)
        wt += self.sa(self.ttm.mlp.fc1.weight) + self.sa(self.ttm.mlp.fc2.weight) + self.sa(self.ttm.lnorm.weight) + self.sa(self.ttm.lnormz.weight) + self.sa(self.ttm.Wo) + self.sa(self.ttm.Wqkv) + self.sa(self.ttm.weight)
        wt += self.sa(self.stm.mlp.fc1.weight) + self.sa(self.stm.mlp.fc2.weight) + self.sa(self.stm.lnorm.weight) + self.sa(self.stm.lnormz.weight) + self.sa(self.stm.Wo) + self.sa(self.stm.Wqkv) + self.sa(self.stm.weight)
        wt += self.sa(self.rtm.mlp.fc1.weight) + self.sa(self.rtm.mlp.fc2.weight) + self.sa(self.rtm.lnorm.weight) + self.sa(self.rtm.lnormz.weight) + self.sa(self.rtm.Wo) + self.sa(self.rtm.Wqkv) + self.sa(self.rtm.weight)
        wt += self.sa(self.odcm.cvf1.weight) + self.sa(self.odcm.cvf2.weight) + self.sa(self.odcm.cvf3.weight)

        ls = -(label * torch.log(xf) + (1 - label) * torch.log(1 - xf))
        ls = torch.mean(ls) + L1_reg_const * wt

        return ls

    def eegloss_light(self, xf, label, L1_reg_const): # takes the weight sum of cnndecoder only
        wt = self.sa(self.cnndecoder.fc.weight) + self.sa(self.cnndecoder.cvd1.weight) + self.sa(self.cnndecoder.cvd2.weight) + self.sa(self.cnndecoder.cvd3.weight)
        ls = -(label * torch.log(xf) + (1 - label) * torch.log(1 - xf))
        ls = torch.mean(ls) + L1_reg_const * wt
        return ls

    def eegloss_wol1(self, xf, label): # without L1
        ls = -(label * torch.log(xf) + (1 - label) * torch.log(1 - xf))
        ls = torch.mean(ls)
        return ls

    # BCE - does not need one hot encoding
    def bceloss(self, xf, label):  # BCE loss
        ls = -(label * torch.log(xf[:,1]) + (1 - label) * torch.log(xf[:,0]))
        ls = torch.mean(ls)
        return ls

    def bceloss_w(self, xf, label, numpos, numtot):  # Weighted BCE loss
        w0 = numtot / (2 * (numtot - numpos))
        w1 = numtot / (2 * numpos)
        ls = -(w1 * label * torch.log(xf[:,1]) + w0 * (1 - label) * torch.log(xf[:,0]))
        ls = torch.mean(ls)
        return ls

    def sa(self, t):
        return torch.sum(torch.abs(t))
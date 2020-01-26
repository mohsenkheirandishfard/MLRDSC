import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import numpy as np
import scipy.io as sio
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
from scipy.sparse.linalg import svds
from sklearn import cluster
from sklearn.preprocessing import normalize
from munkres import Munkres
import os, random
import yaml


if torch.cuda.is_available():
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    print("running on GPU")
else:
    print("running on CPU")

# ===========================================================================================
# ======================================= NN Model ==========================================
# ===========================================================================================
class ConvAE(nn.Module):
    def __init__(self, params):
        super(ConvAE, self).__init__()
        kernelSize = params["kernelSize"]
        numHidden = params["numHidden"]
        cte = params["cte"]

        self.batchSize = params["numSubj"] * params["numPerSubj"]

        self.padEncL1 = nn.ZeroPad2d((1, 2, 1, 2))
        self.encL1 = nn.Conv2d(1, numHidden[0], kernel_size=kernelSize[0], stride=2)

        self.padEncL2 = nn.ZeroPad2d((1, 1, 2, 1))
        self.encL2 = nn.Conv2d(numHidden[0], numHidden[1], kernel_size=kernelSize[1], stride=2)
        self.padEncL2p = nn.ZeroPad2d((0, 0, -1, 0))

        self.padEncL3 = nn.ZeroPad2d((1, 1, 2, 1))
        self.encL3 = nn.Conv2d(numHidden[1], numHidden[2], kernel_size=kernelSize[2], stride=2)
        self.padEncL3p = nn.ZeroPad2d((0, 0, -1, 0))

        c = cte * np.ones((self.batchSize, self.batchSize))
        self.C = nn.Parameter(Variable(torch.Tensor(c), requires_grad=True))
        self.D1 = nn.Parameter(Variable(torch.Tensor(c), requires_grad=True))
        self.D2 = nn.Parameter(Variable(torch.Tensor(c), requires_grad=True))
        self.D3 = nn.Parameter(Variable(torch.Tensor(c), requires_grad=True))

        self.decL1 = nn.ConvTranspose2d(numHidden[2], numHidden[1], kernel_size=kernelSize[2], stride=2)
        self.padDecL1 = nn.ZeroPad2d((-1, -1, 0, -1))
        self.decL2 = nn.ConvTranspose2d(numHidden[1], numHidden[0], kernel_size=kernelSize[1], stride=2)
        self.padDecL2 = nn.ZeroPad2d((-1, -1, 0, -1))
        self.decL3 = nn.ConvTranspose2d(numHidden[0], 1, kernel_size=kernelSize[0], stride=2)
        self.padDecL3 = nn.ZeroPad2d((-1, -2, -1, -2))

    def forward(self, X):
        Z1 = F.relu(self.encL1(self.padEncL1(X)))
        Z2 = F.relu(self.padEncL2p(self.encL2(self.padEncL2(Z1))))
        Z3 = F.relu(self.padEncL3p(self.encL3(self.padEncL3(Z2))))

        Y3 = (self.C + self.D1).mm(Z3.view(self.batchSize, -1))
        Y3 = Y3.view(Z3.size())

        Y2 = (self.C + self.D2).mm(Z2.view(self.batchSize, -1))
        Y2 = Y2.view(Z2.size())

        Y1 = (self.C + self.D3).mm(Z1.view(self.batchSize, -1))
        Y1 = Y1.view(Z1.size())

        O3 = F.relu(self.padDecL1(self.decL1(Y3)))
        O2 = F.relu(self.padDecL2(self.decL2(O3 + Y2)))
        output = F.relu(self.padDecL3(self.decL3(O2 + Y1)))

        return Z3, Y3, Z2, Y2, Z1, Y1, self.C, self.D1, self.D2, self.D3, output

# ===========================================================================================
# ========================== Subspace Clustering Method MLRDSC ==============================
# ===========================================================================================
def subspaceClusteringMLRDSC(images, params):
    numSubjects = params["numSubj"]
    numPerSubj  = params["numPerSubj"]
    numEpochs   = params["numEpochs"]
    fileName    = params["preTrainedModel"]
    alpha = params["alpha"]
    lr    = params["lr"]
    T     = params["T"]
    cte   = params["cte"]
    seedValue = params["seedValue"]
    batchSize = params["numSubj"] * params["numPerSubj"]
    regparams = params["regparams"]
    label = params["label"]
    lambda1, lambda2, lambda3 = params["lambdas"][0], params["lambdas"][1], params["lambdas"][2]

    if params["seedFlag"]:
        random.seed(seedValue)
        os.environ['PYTHONHASHSEED'] = str(seedValue)
        np.random.seed(seedValue)
        torch.manual_seed(seedValue)
        torch.cuda.manual_seed(seedValue)
        torch.cuda.manual_seed_all(seedValue)  # if you are using multi-GPU.
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    faceSubjects = np.array(images[0:numPerSubj * (numSubjects), :])
    faceSubjects = faceSubjects.astype(float)
    labelSubjects = np.array(label[0:numPerSubj * (numSubjects)])
    labelSubjects = labelSubjects - labelSubjects.min() + 1
    labelSubjects = np.squeeze(labelSubjects)

    X = Variable(torch.Tensor(np.transpose(faceSubjects, (0, 1, 2, 3))).cuda(), requires_grad=False)

    CAE = ConvAE(params)
    CAE = CAE.cuda()

    preTrainedMod = torch.load(fileName)
    parametersAE = dict([(name, param) for name, param in preTrainedMod.named_parameters()])

    for name, param in CAE.named_parameters():
        if name in parametersAE:
            param_pre  = parametersAE[name]
            param.data = param_pre.data

    CAE.C.data = (torch.Tensor(cte * np.ones((batchSize, batchSize)))).cuda()
    CAE.D1.data = (torch.Tensor(cte * np.ones((batchSize, batchSize)))).cuda()
    CAE.D2.data = (torch.Tensor(cte * np.ones((batchSize, batchSize)))).cuda()
    CAE.D3.data = (torch.Tensor(cte * np.ones((batchSize, batchSize)))).cuda()

    optimizer = torch.optim.Adam(CAE.parameters(), lr=lr)

    Q = Variable(nn.init.constant(torch.empty(batchSize, numSubjects), 0.0), requires_grad=True).cuda()
    numSamples = faceSubjects.shape[0]

    for epoch in range(numEpochs + 1):
        Z3, Y3, Z2, Y2, Z1, Y1, C, D1, D2, D3, output = CAE(X)

        regLoss = lambda2 * torch.norm(torch.mm(torch.transpose(Q, 1, 0), torch.abs(C)), p=1) + \
                  lambda3 * (torch.norm(D1, p=2) ** 2 + torch.norm(D2, p=2) ** 2 + torch.norm(D3, p=2) ** 2)

        reconLoss = torch.sum((output - X) ** 2)
        expLoss = lambda1 * (torch.sum((Z3 - Y3) ** 2) + torch.sum((Z2 - Y2) ** 2) + torch.sum((Z1 - Y1) ** 2))
        loss = reconLoss + regLoss + regparams * expLoss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        C = C * (torch.ones(batchSize, batchSize) - torch.eye(batchSize, batchSize)).cuda()
        D1 = D1 * (torch.ones(batchSize, batchSize) - torch.eye(batchSize, batchSize)).cuda()
        D2 = D2 * (torch.ones(batchSize, batchSize) - torch.eye(batchSize, batchSize)).cuda()
        D3 = D3 * (torch.ones(batchSize, batchSize) - torch.eye(batchSize, batchSize)).cuda()

        if epoch % T == 0:
            print("Losses  "+"Reconstruction: %.8f     Expression: %.8f     Regularization: %.8f" % (reconLoss / numSamples, expLoss, regLoss))
            Coef = thrC(C.detach().cpu().numpy() + (1. / 3) * (D1 + D2 + D3).detach().cpu().numpy(), alpha)
            yHat, _ = post_proC(Coef, labelSubjects.max(), params)
            Q.data = torch.Tensor(np.eye(numSubjects)[np.array(yHat.astype(int)).reshape(-1) - 1]).cuda()
            errorClus = err_rate(labelSubjects, yHat)
            accuClus = 1 - errorClus
            print("Accuracy after %d" % (epoch), "Iterations: %.4f" % accuClus)

    return (1 - accuClus), (1 - accuClus)


def best_map(L1, L2):
    # L1 should be the groundtruth labels and L2 should be the clustering labels we got
    Label1 = np.unique(L1)
    nClass1 = len(Label1)
    Label2 = np.unique(L2)
    nClass2 = len(Label2)
    nClass = np.maximum(nClass1, nClass2)
    G = np.zeros((nClass, nClass))
    for i in range(nClass1):
        ind_cla1 = L1 == Label1[i]
        ind_cla1 = ind_cla1.astype(float)
        for j in range(nClass2):
            ind_cla2 = L2 == Label2[j]
            ind_cla2 = ind_cla2.astype(float)
            G[i, j] = np.sum(ind_cla2 * ind_cla1)
    m = Munkres()
    index = m.compute(-G.T)
    index = np.array(index)
    c = index[:, 1]
    newL2 = np.zeros(L2.shape)
    for i in range(nClass2):
        newL2[L2 == Label2[i]] = Label1[c[i]]
    return newL2

def thrC(C, ro):
    if ro < 1:
        N = C.shape[1]
        Cp = np.zeros((N, N))
        S = np.abs(np.sort(-np.abs(C), axis=0))
        Ind = np.argsort(-np.abs(C), axis=0)
        for i in range(N):
            cL1 = np.sum(S[:, i]).astype(float)
            stop = False
            csum = 0
            t = 0
            while stop == False:
                csum = csum + S[t, i]
                if csum > ro * cL1:
                    stop = True
                    Cp[Ind[0:t + 1, i], i] = C[Ind[0:t + 1, i], i]
                t = t + 1
    else:
        Cp = C
    return Cp

def post_proC(C, K, params):
    # C: Cicient matrix, K: number of clusters, d: dimension of each subspace
    d = params["post_proc"][0]
    alpha = params["post_proc"][1]

    C = 0.5 * (C + C.T)
    # r = d * K + 1
    r = min(d * K + 1, C.shape[0] - 1)
    U, S, _ = svds(C, r, v0=np.ones(C.shape[0]))
    U = U[:, ::-1]
    S = np.sqrt(S[::-1])
    S = np.diag(S)
    U = U.dot(S)
    U = normalize(U, norm='l2', axis=1)
    Z = U.dot(U.T)
    Z = Z * (Z > 0)
    L = np.abs(Z ** alpha)
    L = L / L.max()
    L = 0.5 * (L + L.T)
    spectral = cluster.SpectralClustering(n_clusters=K, eigen_solver='arpack', affinity='precomputed',
                                          assign_labels='discretize')
    spectral.fit(L)
    grp = spectral.fit_predict(L) + 1
    return grp, L

def err_rate(gt_s, s):
    c_x = best_map(gt_s, s)
    err_x = np.sum(gt_s[:] != c_x[:])
    missrate = err_x.astype(float) / (gt_s.shape[0])
    return missrate

# ===========================================================================================
# ====================================== Main Function ======================================
# ===========================================================================================
if __name__ == "__main__":

    args = yaml.load(open("EYaleB_config.yaml", 'r'))
    params = {}
    params["numSubj"]    = args["dataset"]["numSubj"]
    params["numPerSubj"] = args["dataset"]["numPerSubj"]
    params["lr"]         = args["training"]["lr"]
    params["T"]          = args["training"]["T"]
    params["numEpochs"]  = args["training"]["numEpochs"]
    params["lambdas"]    = args["training"]["lambdas"]
    params["cte"]        = args["training"]["cte"]
    params["seedFlag"]   = args["training"]["seedFlag"]
    params["seedValue"]  = args["training"]["seedValue"]
    params["post_proc"]  = args["training"]["post_proc"]
    params["dataPath"]   = args["dataset"]["dataPath"]
    params["preTrainedModel"] = args["model"]["preTrainedModel"]
    params["kernelSize"] = args["model"]["kernelSize"]
    params["numHidden"]  = args["model"]["numHidden"]
    params["input_size"] = args["model"]["input_size"]

    params["alpha"]      = max(0.4 - (params["numSubj"] - 1) / 10 * 0.1, 0.1)
    params["regparams"]  = 1.0 * 10 ** (params["numSubj"] / 10.0 - 3.0)
    data   = sio.loadmat(params["dataPath"])

    images = data['Y']
    I     = []
    label = []

    for i in range(images.shape[2]):
        for j in range(images.shape[1]):
            temp = np.reshape(images[:, j, i], params["input_size"])
            label.append(i)
            I.append(temp)
    I      = np.array(I)
    label  = np.array(label[:])
    images = np.transpose(I, [0, 2, 1])
    images = np.reshape(images, [images.shape[0], 1, images.shape[1], images.shape[2]])

    params["label"] = label
    errorMean, errorMed = subspaceClusteringMLRDSC(images, params)

    print("====================================================")
    print('%d subjects:' % params["numSubj"])
    print('Mean: %.4f%%' % (errorMean * 100), 'Median: %.4f%%' % (errorMed * 100))
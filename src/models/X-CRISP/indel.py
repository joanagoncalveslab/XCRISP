import os, sys
import pandas as pd
import numpy as np
from torch.nn.modules.loss import NLLLoss
from tqdm import trange

import torch
from torch import nn
from torch.nn.functional import mse_loss, binary_cross_entropy
from torch.multiprocessing import Process
from torch.utils.tensorboard import SummaryWriter
from audtorch.metrics.functional import pearsonr
from sklearn.model_selection import KFold, train_test_split

sys.path.append("../")
from data_loader import get_common_samples
from test_setup import MIN_NUMBER_OF_READS

class IndelNeuralNetwork(nn.Module):
    def __init__(self):
        super(IndelNeuralNetwork, self).__init__()
        self.linear_sigmoid_stack = nn.Sequential(
            nn.Linear(384, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        out = self.linear_sigmoid_stack(x)
        return out

def _to_tensor(arr):
    if isinstance(arr, pd.DataFrame) or isinstance(arr, pd.Series):
        arr = arr.to_numpy()
    return torch.tensor(arr).to(DEVICE).float()

def get_folds(s):
    kf = KFold(n_splits=NUM_FOLDS, random_state=RANDOM_STATE, shuffle=True)
    folds = list(kf.split(s))
    return folds

def batch(X, Y, samples, model, optimizer, lr_scheduler):
    loss = torch.zeros(1).to(DEVICE)
    x = _to_tensor(X.loc[samples])
    y = _to_tensor(Y.loc[samples])
    y_pred = model(x)[:,0]

    loss += binary_cross_entropy(y_pred, y)

    # loss += cross_entropy(y_pred, torch.max(y, 1)[1]) # nll loss (for use with LogSoftmax)
    # loss = torch.div(loss, len(samples))

    # Backpropagation
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    lr_scheduler.step()

    return np.round(loss.cpu().detach().numpy()[0], 4)

def load_data(dataset = "train", num_samples = None, fractions=True):
    input_f = OUTPUT_DIR + "/model_training/data_{}x".format(MIN_READS_PER_TARGET) + "/X-CRISP/{}.pkl" 
    data = pd.read_pickle(input_f.format(dataset))
    counts = data["counts"]
    counts = counts.loc[counts["Type"] == "INSERTION"]
    samples = counts.index.levels[0]
    features = data["ins_features"]
    if num_samples is not None:
        samples = samples[:num_samples]
    X = features.loc[samples]
    counts = counts.loc[samples]
    y = counts.fraction if fractions else counts.countEvents
    y = y.unstack(level=-1)
    y = y.sum(axis=1)
    return X, y, samples

def init_model(l2):
    model = IndelNeuralNetwork().to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), 0.0005, betas=(0.99, 0.999), weight_decay=l2)
    lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer, gamma=0.99)
    return model, optimizer, lr_scheduler

def test(X, Y, samples, model):
    model.eval()
    mse_metrics = []
    with torch.no_grad():
        x = _to_tensor(X.loc[samples])
        y_pred = model(x)
        y = _to_tensor(Y.loc[samples])
        mse_metrics.append(mse_loss(y_pred, y).cpu().detach().numpy())
    return np.round(np.mean(mse_metrics), 4)


def train(X, y, samples, val_samples, l2, fold, model, optimizer, lr_scheduler):
    train_metric = test(X, y, samples, model)
    print("Starting training correlation: {}".format(train_metric))
    pbar = trange(EPOCHS, desc = "Training ...", leave=True)
    for t in pbar:
        permutation = torch.randperm(len(samples))

        for i in range(0, len(samples), BATCH_SIZE):
            samples_batch = samples[permutation[i:i+BATCH_SIZE]]
            loss = batch(X, y, samples_batch, model, optimizer, lr_scheduler)
            train_metric = test(X, y, samples_batch, model)
            val_metric = test(X, y, val_samples, model)
            pbar.set_description("L2: {}, Fold: {}, Epoch {}, Train loss: {:.5f}, Train Pearson's Corr: {:.5f}, Validation Pearson's Corr: {:.5f}".format(l2, fold, t, loss, train_metric, val_metric))
    print("Finished training insertion model")

    return loss, train_metric, val_metric

def run_experiment(X, y, samples):
    lambdas = 10 ** np.arange(-10, -1, 0.1)
    lambda_errs = np.zeros(lambdas.shape)
    # cross validation
    folds = get_folds(samples)
    for i, l2 in enumerate(lambdas):
        cv_folds = []
        for j, f in enumerate(folds):
            print("Training fold {} with {} samples, {} features".format(j, len(samples[f[0]]), X.shape[1]))
            model, optimizer, lr_scheduler = init_model(l2)
            loss, train_corr, val_corr = train(X, y, samples[f[0]], samples[f[1]], l2, j, model, optimizer, lr_scheduler)
            cv_folds.append(val_corr)
        lambda_errs[i] = np.mean(cv_folds)
    
    best_lambda = lambdas[np.argmin(lambda_errs)]

    # final training
    model, optimizer, lr_scheduler = init_model(best_lambda)
    samples, val_samples = train_test_split(samples, test_size = 100, random_state=RANDOM_STATE) # hold out 100 
    train(X, y, samples, val_samples, best_lambda, "final", model, optimizer, lr_scheduler)

    # save model
    os.makedirs(OUTPUT_MODEL_D, exist_ok=True)
    torch.save(model, OUTPUT_MODEL_F)
    torch.save({
        "model": model.state_dict(),
        "samples": samples,
        "optimiser" : optimizer.state_dict(),
        "lr_scheduler": lr_scheduler.state_dict()
    }, OUTPUT_MODEL_F.replace("pth", "details"))

def load_model(model_dir="./models/"):
    model_d = "{}indel_model.pth".format(model_dir)
    model = torch.load(model_d)
    return model


# set global vars
OUTPUT_DIR = os.environ['OUTPUT_DIR']
MIN_READS_PER_TARGET = MIN_NUMBER_OF_READS
DEVICE = "cpu"
TRAIN_GENOTYPE = "0105-mESC-Lib1-Cas9-Tol2-BioRep2-techrep1"

if __name__ == "__main__":
    LOGS_DIR = os.environ['LOGS_DIR']
    OUTPUT_MODEL_D = OUTPUT_DIR + "/model_training/model/X-CRISP/"
    OUTPUT_MODEL_F = OUTPUT_MODEL_D + "indel_model.pth"
    NUM_FOLDS = 5
    RANDOM_STATE = 1
    EPOCHS = 100
    BATCH_SIZE = 200
    # get devices
    # DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
    DEVICE = "cpu"
    print('Using {} device'.format(DEVICE))

    # set number of threads to 1 for various libraries
    os.environ['OPENBLAS_NUM_THREADS'] = '1'
    os.environ['MKL_NUM_THREADS'] = '1'
    os.environ['OMP_NUM_THREADS'] = '1'

    # set random seed
    torch.manual_seed(RANDOM_STATE)
    np.random.seed(RANDOM_STATE)
    
    # load data
    X, y, samples = load_data(dataset=TRAIN_GENOTYPE, num_samples=None)

    # use common samples for experiment consistency
    common_samples = get_common_samples(genotype=TRAIN_GENOTYPE, min_reads=MIN_READS_PER_TARGET)
    samples = np.intersect1d(samples, common_samples)

    print("Training on {} samples, with {} features".format(len(samples), X.shape[1]))
    run_experiment(X, y, samples)

    print("Finished.")



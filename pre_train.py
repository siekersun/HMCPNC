import random
import os
import sys
import torch
import logging
import time
from torch.utils.tensorboard import SummaryWriter
import numpy as np
from tqdm import tqdm
import argparse
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pandas as pd
from data.dataset_loader import dataset_loader
from torch_geometric.loader import DataLoader
from sklearn.metrics import precision_recall_fscore_support
from model.pretrain_model import LungPrediction
from args import args
from sklearn.metrics import precision_recall_fscore_support, roc_auc_score, average_precision_score, confusion_matrix


def get_accuracy_from_logits(logits, labels):
    """计算准确率"""
    probs = torch.argmax(logits, dim=1)
    acc = (probs == labels).float().mean()
    return acc


def evaluate(model, dev_loader):
    """评估"""
    mean_acc, mean_loss = 0, 0
    count = 0
    test_correct = 0
    test_predictions = []
    test_targets = []
    all_probs = []  # 用于保存模型的预测概率
    with torch.no_grad():
        for i, data in enumerate(dev_loader):
            image, clinical, p, y = data[0].cuda(), data[1].cuda(), data[2].cuda(), data[3].cuda()
            output, _ = model(image, clinical, p)

            # 获取预测标签和概率
            probs = torch.softmax(output, dim=1)[:, 1]  # 假设 pos_label=1
            _, predicted = torch.max(output, dim=1)

            test_correct += (predicted == y).sum().item()
            mean_loss += nn.functional.cross_entropy(output, y, reduction='mean').item()
            mean_acc += get_accuracy_from_logits(output, y)
            count += 1

            test_predictions.extend(predicted.cpu().numpy())
            test_targets.extend(y.cpu().numpy())
            all_probs.extend(probs.cpu().numpy())  # 保存概率

    # 计算评估指标
    p, r, f, _ = precision_recall_fscore_support(test_targets, test_predictions, pos_label=1, average="macro")
    auroc = roc_auc_score(test_targets, all_probs)  # 计算 AUROC
    auprc = average_precision_score(test_targets, all_probs)  # 计算 AUPRC
    tn, fp, fn, tp = confusion_matrix(test_targets, test_predictions).ravel()
    specificity = tn / (tn + fp)

    return mean_acc / count, mean_loss / count, p, r, f, specificity, auroc, auprc


if __name__ == "__main__":

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    db_train = dataset_loader(base_dir=args.root_path, dataset_class='Training', num_classes=args.num_classes)
    db_test = dataset_loader(base_dir=args.root_path, dataset_class='Internal test', num_classes=args.num_classes)
    Exter_test = dataset_loader(base_dir=args.root_path, dataset_class='External test', num_classes=args.num_classes)
    print("The length of train set is: {}".format(len(db_train)))
    print("The length of test set is: {}".format(len(db_test)))
    print("The length of test set is: {}".format(len(Exter_test)))


    def worker_init_fn(worker_id):
        random.seed(args.seed + worker_id)


    trainloader = DataLoader(db_train, batch_size=args.batch_size, shuffle=False, num_workers=0, pin_memory=True,
                             worker_init_fn=worker_init_fn, drop_last=False)
    testloader = DataLoader(db_test, batch_size=1, shuffle=False, num_workers=1)
    Exterloader = DataLoader(Exter_test, batch_size=1, shuffle=False, num_workers=1)

    # net = StandardNet_Simple().cuda()
    net = LungPrediction(input_size=9, hidden_size=args.hidden_size, phi=args.phi,
                         layer_num=args.layer_num, pre_train=args.pre_train, num_class=2).cuda()
    # net = FeatureNet().cuda()
    optimizer_net = optim.Adam(net.parameters(), lr=args.base_lr,weight_decay=0.01)
    # cosine_schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer_net, T_max=16, eta_min=0.1*args.base_lr)
    cosine_schedule = optim.lr_scheduler.CyclicLR(optimizer_net, base_lr=0.05 * args.base_lr, max_lr=args.base_lr,
                                                  gamma=0.95, mode="exp_range", step_size_up=8,
                                                  cycle_momentum=False)

    net.train()

    st = time.time()
    output_dir = os.path.join(args.output_dir, args.output_name)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    writer = SummaryWriter(output_dir + '/log')

    timestr = time.strftime("%Y-%m-%d-%H-%M-%S_", time.localtime(time.time()))
    logging.basicConfig(filename=output_dir + "/" + timestr + ".txt", level=logging.INFO,
                        format='[%(asctime)s.%(msecs)03d] %(message)s', datefmt='%H:%M:%S')
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    logging.info(str(args))
    # logging.info("train: {}; test: {};".format(len(trainloader), len(testloader)))

    max_iterations = args.max_epoch * len(trainloader)  # max_epoch = max_iterations // len(trainloader) + 1
    logging.info("{} iterations per epoch. {} max iterations ".format(len(trainloader), max_iterations))


    CEloss = nn.CrossEntropyLoss()

    iterator = range(args.max_epoch)
    iter_num = 0

    Inter_acc_best = 0
    Exter_acc_best = 0
    acc_best = 0

    for epoch_num in iterator:
        running_loss, running_acc = 0, 0
        net.train()
        train_targets = []
        train_probs = []

        for batch_idx, data in enumerate(tqdm(trainloader, desc=f"Epoch {epoch_num}", leave=True)):
            image, clinical, p, y = data[0].cuda(), data[1].cuda(), data[2].cuda(), data[3].cuda()
            output, _ = net(image, clinical, p)

            loss_ce = CEloss(output, y)

            total_loss = loss_ce
            optimizer_net.zero_grad()
            total_loss.backward()
            optimizer_net.step()

            running_acc_iter = get_accuracy_from_logits(output, y)
            running_acc += running_acc_iter
            running_loss += loss_ce.item()
            # 获取预测标签和概率
            probs = torch.softmax(output, dim=1)[:, 1]  #
            train_targets.extend(y.cpu().numpy())
            train_probs.extend(probs.detach().cpu().numpy())

            iter_num = iter_num + 1
            # tqdm.write('iteration %d : loss : %f ; train_acc : %f ;' % (iter_num, loss_ce.item(), running_acc_iter))
            writer.add_scalar('info/loss_ce', loss_ce, iter_num)
            writer.add_scalar('info/train_acc', running_acc_iter, iter_num)

        cosine_schedule.step()
        train_auroc = roc_auc_score(train_targets, train_probs)  # 计算 AUROC
        train_auprc = average_precision_score(train_targets, train_probs)  # 计算 AUPRC
        logging.info(
            f"epoch {epoch_num}: train_loss: {running_loss / len(trainloader):.6f}, accuracy: {running_acc / len(trainloader):.6f}; "
            f"Time taken (s): {(time.time() - st):.6f}, train_auroc: {train_auroc:.6f}, train_auprc: {train_auprc:.6f}")
        st = time.time()
        net.eval()

        Inter_acc, Inter_loss, p, r, f, Inter_spec, Intest_auroc, Intest_auprc = evaluate(net, testloader)
        if Inter_acc > Inter_acc_best:
            Inter_acc_best = Inter_acc
            logging.info('save best Inter model')
            save_path = f'{output_dir}/Internal test_model.pt'
            torch.save(net.state_dict(),save_path)
        logging.info(
            'epoch %d: Inter_acc : %f Inter_loss : %f Inter_precise : %f Inter_recall : %f Inter_f1 : %f Inter_spec: %f Inter_auroc : %f Inter_auprc : %f' % (
                epoch_num, Inter_acc, Inter_loss, p, r, f, Inter_spec, Intest_auroc, Intest_auprc))

        Exter_acc, Exter_loss, Exter_p, Exter_r, Exter_f, Exter_spec, Exter_auroc, Exter_auprc = evaluate(net, Exterloader)
        if Exter_acc > Exter_acc_best:
            Exter_acc_best = Exter_acc
            logging.info('save best Exter model')
            save_path = f'{output_dir}/External test_model.pt'
            torch.save(net.state_dict(),save_path)
        logging.info(
            'epoch %d: Exter_acc : %f Exter_loss : %f Exter_precise : %f Exter_recall : %f Exter_f1 : %f Exter_spec: %f Exter_auroc : %f Exter_auprc : %f' % (
                epoch_num, Exter_acc, Exter_loss, Exter_p, Exter_r, Exter_f, Exter_spec, Exter_auroc, Exter_auprc))

    writer.close()




import os
import argparse
import torch
from torch import nn
import torch.backends.cudnn as cudnn
import time
import pandas as pd

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import random 
import openslide
import skimage.measure
from PIL.ImageOps import invert

from sklearn.model_selection import train_test_split
from torchvision.transforms import transforms
from torch.utils.data import Dataset, DataLoader
import torch.optim as optim

#import pretrainedmodels
from efficientnet_pytorch import EfficientNet
from sklearn.metrics import cohen_kappa_score
    
def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
set_seed(42)

parser = argparse.ArgumentParser(
    description='Prostate Cancer Grader')
parser.add_argument('--root', default='..',
                    type=str, help='directory of the data')
parser.add_argument('--batch_size', default=8, type=int,
                    help='Batch size for training')
parser.add_argument('-w','--workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--lr', default=0.01, type=float,
                    help='initial learning rate')
parser.add_argument('-e','--epochs', default=24, type=int,
                    help='number of epochs to train')
parser.add_argument('-o','--output_folder', default='save/', type=str,
                    help='Dir to save results')
parser.add_argument('-wd','--weight_decay', default=1e-5, type=float,
                    help='Weight decay')
parser.add_argument('-c','--checkpoint', default=None, type=str,
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('-r','--resume_epoch', default=0, type=int,
                    help='epoch number to be resumed at')
parser.add_argument('-s','--size', default=128, type=int,
                    help='image size for training, divisible by 64')
parser.add_argument('-ls','--log_step', default=10, type=int,
                    help='number of steps to print log')
parser.add_argument('--step', default=4, type=int,
                    help='step to reduce lr')
parser.add_argument('-a','--arch', default='efficientnet-b4', type=str,
                    help='architecture of EfficientNet')

args = parser.parse_args()



if torch.cuda.is_available():
    device = torch.device("cuda:0")
    torch.set_default_tensor_type(torch.cuda.HalfTensor)
    torch.cuda.set_device(device)
    cudnn.benchmark = True
else:
    device = torch.device("cpu")

if not os.path.exists(args.output_folder):
    os.mkdir(args.output_folder)
    

nlabel = 6
transform = {}
transform['train'] = transforms.Compose([
     transforms.RandomVerticalFlip(),
     transforms.RandomHorizontalFlip(),
     transforms.ToTensor(),
     transforms.Normalize(mean=[0.485, 0.456, 0.406],
                          std=[0.229, 0.224, 0.225]),
     ])
transform['val'] = transforms.Compose([
     transforms.ToTensor(),
     transforms.Normalize(mean=[0.485, 0.456, 0.406],
                          std=[0.229, 0.224, 0.225]),
     ])
transform['test'] = transform['val'] 
'''
extract_images("001c62abd11fa4b57bf7a6c603a11bb9",
    "/kaggle/input/prostate-cancer-grade-assessment/train_images",
    256,
    False)
'''      
def topk(X, n):
    x = np.zeros(n, dtype=int)
    y = np.zeros(n, dtype=int)
    x_ = np.ravel(X)
    for i in range(n):
        ij = x_.argmax()
        x_[ij] = 0
        x[i], y[i] = np.unravel_index(ij, X.shape)
    return x, y
      
def extract_images(img_id, img_dir, size, mode, debug=False):
    image_path = os.path.join(img_dir, img_id + '.tiff')
    image = openslide.OpenSlide(image_path)
    w0,h0 = image.level_dimensions[0]
    view = (64,64)
    thumbnail = invert(image.get_thumbnail(view))  
    img = np.array(thumbnail).mean(2)
    w1,h1 = thumbnail.size
    num =  {32:24}
    images = []
    if debug:
        fig,ax = plt.subplots(1)
        ax.imshow(img)
    for level, n in num.items():
        r = 64 // level
        label = skimage.measure.block_reduce(img, (r,r), np.mean)
        xs,ys = topk(label,30)
        ll=list(range(30))
        if mode == 'train':random.shuffle(ll)
        ll = ll[:n]
        pts = [(x,y) for x,y in zip(xs[ll],ys[ll])]
        for x,y in pts:
            s0 = max(w0,h0)
            l = image.get_best_level_for_downsample(s0//(level*size))
            s = max(image.level_dimensions[l])
            ix,iy = x*s0//level , y*s0//level
            crop_size = s//level
            if mode == 'train':
                t = s0//level//8
                ix += random.randrange(-t, t)
                iy += random.randrange(-t, t)
                crop_size = int(random.uniform(0.8*crop_size,1.2*crop_size))
            im = image.read_region((iy,ix), l, (crop_size,crop_size))    
           
            if debug:
                rect = patches.Rectangle((y*r,x*r),r,r,linewidth=1,edgecolor='r',facecolor='none')
                ax.add_patch(rect)
            im = im.resize((size,size)).convert('RGB')
            images += [im]

    if debug:
        for im in images:
            plt.figure()
            plt.imshow(np.array(im))
    
    return images


provider_label = {"radboud":0., "karolinska":1.}
class ProstateData(Dataset):
    def __init__(self, df, data_dir, mode, size, transform):
        self.img_dir = os.path.join(data_dir,"train_images")
        self.size = size
        self.mode = mode
        self.transform = transform
        self.df = df
    def __len__(self):
        return len(self.df)
    
    def __getitem__(self, idx):
        img_id = self.df.iloc[idx].image_id
        provider = self.df.iloc[idx].data_provider 
        plab = provider_label[provider]
        plab =  torch.tensor(plab, dtype=torch.float32, device="cpu"), 
        label = self.df.iloc[idx].isup_grade
        
        images = extract_images(img_id, self.img_dir, self.size, self.mode)
        image_tensor =torch.cat(
                (self.transform(im).unsqueeze(0) for im in images),
                dim=0)
        if self.mode == 'train' or self.mode == 'val':
            return image_tensor, plab, torch.tensor(label, dtype=torch.long, device="cpu")
        else:
            return image_tensor, plab


class Grader(nn.Module):
    def __init__(self, n=256, o=nlabel):
        super(Grader, self).__init__()
        self.n = n
        self.model = EfficientNet.from_pretrained(args.arch)
        self.model._fc = nn.Linear(self.model._fc.in_features, n*3)
        self.act = nn.GELU()
        self.norm = nn.LayerNorm([17,n])
        encoder_layer  = nn.TransformerEncoderLayer(n+1, 8)
        self.attention = nn.TransformerEncoder(encoder_layer, num_layers=3)
        self.fc = nn.Linear(n,o)
    def forward(self,x,p): # batch x 17 x size x size x 3
        b, n, c, w, h = x.shape
        x = self.model(x.view(b*n, c, w, h))
        x = x.view(b,n,-1)
        x = self.norm(self.act(x)).view(b,n,-1)
        x = torch.cat((x,p),dim=-1)
        x = self.attention(x)[:,0,:]
        x = self.fc(x) # b x 1 x o 
        return x
    
def main():
    train_csv = pd.read_csv(os.path.join(args.root, "train.csv"))
    df = {}
    df['train'], df['val'] = train_test_split(train_csv, test_size=0.05, random_state=42)
    dataset = {x: ProstateData(df[x], args.root, x, args.size, transform=transform[x]) 
                for x in ['train', 'val']}
    loader={x: DataLoader(dataset[x],
                          batch_size=args.batch_size, 
                          shuffle= (x=='Train'),
                          num_workers=args.workers,
                          pin_memory=True)
                  for x in ['train', 'val']}
    
    model = Grader()
    if torch.cuda.is_available():
        model=nn.DataParallel(model)

    if args.checkpoint:
        print('Resuming training from epoch {}, loading {}...'
              .format(args.resume_epoch,args.checkpoint))
        weight_file=os.path.join(args.root,args.checkpoint)
        model.load_state_dict(torch.load(weight_file,
                                 map_location=lambda storage, loc: storage))
    """
    model.to(device).half()
    for layer in model.modules():
        if isinstance(layer, nn.BatchNorm2d):
            layer.float()
    """
    num_class = np.array(train_csv.groupby('isup_grade').count().image_id)        
    class_weights = np.power(num_class.max()/num_class, 1.1)
    print("class weights:",class_weights)
    class_weights = torch.tensor(class_weights, dtype=torch.float16, device=device)
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = torch.optim.SGD(model.parameters(),lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=args.step, gamma=0.1)
    
    for i in range(args.resume_epoch):
        scheduler.step()
    
    for epoch in range(args.resume_epoch, args.epochs):
        for phase in ['train','val']:
            t0 = time.time()
            print("========={}:{}=========".format(phase,epoch))
            if phase == 'train':
                model.train()
            else:
                model.eval()
            num = 0
            correct = 0
            nums = np.zeros(6,dtype=int)
            corrects = np.zeros(6,dtype=int)
            props = np.zeros(6,dtype=int)
            running_loss=0
            preds = None
            truth = None
            for i, (img, plab, targets) in enumerate(loader[phase]):
                img = img.to(device)
                b, n, _, _, _ = img.shape
                plab = plab.to(device).unsqueeze(-1).expand((b,n)).unsqueeze(-1)
                targets= targets.to(device)
                optimizer.zero_grad()
                with torch.set_grad_enabled(phase == 'train'):
                    output = model(img, plab)
                    loss = criterion(output, targets)
                    if phase == 'train':
                        loss.backward()
                        optimizer.step()
                    
                    num += targets.size(0)
                    pred = output.argmax(dim=1)
                    #pred = output.max(1, keepdim=True)[1]
                    correct += pred.eq(targets).sum().item()
                    running_loss += loss.item() * targets.size(0)
                    acc = 100.0 * correct / num
                    if (i+1) % args.log_step == 0:
                        s = "({},{:.1f}s) Loss:{:.3f} Acc:{:.3f}" 
                        print(s.format(num, (time.time()-t0)/(i+1), loss.item(), acc))
                    if phase == 'val':
                        for i in range(nlabel):
                            t = targets.eq(i)
                            p = preds.eq(i)
                            nums[i] += t.sum().item()
                            props[i] += p.sum().item()
                            corrects[i] += (p&t).sum().item()
                            if preds is None:
                                preds = pred.cpu().numpy()
                                truth = targets.cpu().numpy()
                            else:
                                preds = np.concatenate((preds, pred.cpu().numpy()))
                                truth = np.concatenate((truth, targets.cpu().numpy()))
            if phase == 'val':
                kappa = cohen_kappa_score(preds,truth)
                recall = corrects/nums
                precision = corrects/props
                print("kappa:{:.3f},".format(kappa) +
                    "|".join(["{:.3f}".format(r) for r in zip(recall)]) +
                    "|".join(["{:.3f}".format(p) for r in zip(precision)])
                    )
                        
            if phase=="train":scheduler.step()
            if epoch % 1 == 0 and phase=="train":
                torch.save(model.state_dict(), 
                           os.path.join(args.output_folder,"checkpoint-{}.pth".format(epoch)))

if __name__ == '__main__':
    main()

# coding: utf-8
import argparse
import time
import math
import os
import torch
import torch.nn as nn
import torch.onnx
import numpy as np

import data
import model as rnn_model

import torch.distributed as dist

parser = argparse.ArgumentParser(description='PyTorch Wikitext-2 RNN/LSTM Language Model')
parser.add_argument('--data', type=str, default='./data/wikitext-2',
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=200,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=40,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str, default='model.pt',
                    help='path to save the final model')
parser.add_argument('--rank', type=int, default=0,
                    help='rank of worker')
parser.add_argument('--world_size', type=int, default=1,
                    help='number of workers')
args = parser.parse_args()

###############################################################################
# Load data
###############################################################################



# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data.cuda()


###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Tensors, to detach them from their history."""
    if isinstance(h, torch.Tensor):
        return h.detach()
    else:
        return tuple(repackage_hidden(v) for v in h)


def init_hidden(model, bsz):
    weight = next(model.parameters())
    if args.model == 'LSTM':
        return (weight.new_zeros(args.nlayers, bsz, args.nhid),
                weight.new_zeros(args.nlayers, bsz, args.nhid))
    else:
        return weight.new_zeros(args.nlayers, bsz, args.nhid)


# get_batch subdivides the source data into chunks of length args.bptt.
# If source is equal to the example output of the batchify function, with
# a bptt-limit of 2, we'd get the following two Variables for i = 0:
# ┌ a g m s ┐ ┌ b h n t ┐
# └ b h n t ┘ └ c i o u ┘
# Note that despite the name of the function, the subdivison of data is not
# done along the batch dimension (i.e. dimension 1), since that was handled
# by the batchify function. The chunks are along dimension 0, corresponding
# to the seq_len dimension in the LSTM.

def get_batch(source, i):
    seq_len = min(args.bptt, len(source) - 1 - i)
    data = source[i:i+seq_len]
    target = source[i+1:i+1+seq_len].view(-1)
    return data, target


def evaluate(data_source, model, criterion, corpus, eval_batch_size):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0.
    ntokens = len(corpus.dictionary)
    hidden = init_hidden(model, eval_batch_size)
    with torch.no_grad():
        for i in range(args.bptt * args.rank, data_source.size(0) - 1, args.bptt * args.world_size):
            data, targets = get_batch(data_source, i)
            output, hidden = model(data, hidden)
            output_flat = output.view(-1, ntokens)
            total_loss += len(data) * criterion(output_flat, targets).item()
            hidden = repackage_hidden(hidden)
    return total_loss / ((len(data_source) - 1) / float(args.world_size))


def train(model, optimizer, criterion, train_data, corpus, epoch, lr):
    bptt = args.bptt if np.random.random() < 0.95 else args.bptt / 2.
    # Prevent excessively small or negative sequence lengths
    seq_len = max(5, int(np.random.normal(bptt, 5)))
    lr2 = optimizer.param_groups[0]['lr']
    optimizer.param_groups[0]['lr'] = lr2 * seq_len / args.bptt

    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0.
    start_time = time.time()
    ntokens = len(corpus.dictionary)
    hidden = init_hidden(model, args.batch_size)
    for batch, i in enumerate(range(args.bptt * args.rank, train_data.size(0) - 1, args.bptt * args.world_size)):
        data, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden = repackage_hidden(hidden)
        optimizer.zero_grad()
        output, hidden = model(data, hidden)
        loss = criterion(output.view(-1, ntokens), targets)
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        optimizer.step()

        total_loss += loss.item()

        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, batch, len(train_data) // (args.bptt * args.world_size), lr,
                elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()

    optimizer.param_groups[0]['lr'] = lr2


def main():
    # Set the random seed manually for reproducibility.
    torch.manual_seed(args.seed)
    torch.cuda.set_device(0)

    corpus = data.Corpus(args.data)
    eval_batch_size = 10
    train_data = batchify(corpus.train, args.batch_size)
    val_data = batchify(corpus.valid, eval_batch_size)
    test_data = batchify(corpus.test, eval_batch_size)

    ntokens = len(corpus.dictionary)
    model = rnn_model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, args.dropout, args.tied).cuda()

    criterion = nn.CrossEntropyLoss()

    args.distributed = args.world_size > 1
    if args.distributed:
        os.environ['MASTER_ADDR'] = "v01"
        os.environ['MASTER_PORT'] = str(13245)

        dist.init_process_group(
            backend="gloo",
            init_method='env://',
            rank=args.rank,
            world_size=args.world_size)

        model = torch.nn.parallel.DistributedDataParallel(model)
        print ("Setup distributed training")

    # Loop over epochs.
    lr = args.lr
    best_val_loss = None
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr)

    if 't0' in optimizer.param_groups[0]:
        print ("Fix: TO here")

    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train(model, optimizer, criterion, train_data, corpus, epoch, lr)
        val_loss = evaluate(val_data, model, criterion, corpus, eval_batch_size)
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time),
                                           val_loss, math.exp(val_loss)))
        print('-' * 89)
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
            optimizer.param_groups[0]['lr'] = lr

    # Run on test data.
    test_loss = evaluate(test_data, model, criterion, corpus, eval_batch_size)
    print('=' * 89)
    print('| End of training | test loss {:5.2f} | test ppl {:8.2f}'.format(
        test_loss, math.exp(test_loss)))
    print('=' * 89)


if __name__ == "__main__":
    main()

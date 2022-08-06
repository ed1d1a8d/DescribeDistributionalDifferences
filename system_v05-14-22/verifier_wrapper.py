import pickle as pkl
import random
import os
import numpy as np
import re
import torch
import tqdm
from collections import defaultdict
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, T5ForConditionalGeneration
from itertools import zip_longest
import json

device = 'cuda' if torch.cuda.is_available() else 'cpu'
max_seq_length = 128
device_count = torch.cuda.device_count()
BSIZE = 2
if device_count == 4:
    BSIZE = 4
t5tok = AutoTokenizer.from_pretrained('t5-small')


def normalize(t):
    return re.sub("'(.+)'", r'\1', t.lower())


def qc2input(d):
    return normalize(d['q'] + '\\n' + d['c'])


class T5ZeroShotClfQA(torch.nn.Module):

    def __init__(self, qa_model_name, max_seq_length = 128, half_precision=False):
        super(T5ZeroShotClfQA, self).__init__()
        self.tokenizer = t5tok
        self.model = T5ForConditionalGeneration.from_pretrained(qa_model_name)
        if half_precision:
            print('Using half precision')
            self.half_precision = half_precision
            self.model = self.model.half()
        if device == 'cuda':
            self.model.to(device)
        self.vocab = self.tokenizer.get_vocab()
        self.yes_id, self.no_id = self.vocab['▁yes'], self.vocab['▁no']
        self.max_seq_length = max_seq_length
        self.lsm = torch.nn.LogSoftmax(dim=-1)

    def create_batch(self, q_dicts):
        input_strings = [qc2input(d) for d in q_dicts]
        input_strings = [normalize(i) for i in input_strings]
        input_dict = self.tokenizer(input_strings, padding=True, return_tensors="pt",
                                    truncation=True, max_length=self.max_seq_length).to(device)
        return input_dict

    def forward(self, input_dict):
        starts = torch.tensor([[self.model.config.decoder_start_token_id]] * len(input_dict['input_ids'])).to(device)
        output = self.model(**input_dict, decoder_input_ids=starts)
        logits = self.lsm(output.logits[:, 0, [self.no_id, self.yes_id]])
        return logits

    def get_logits_from_input_dict_(self, input_strings):
        input_dict = self.create_batch(input_strings)
        return self.forward(input_dict)

    def get_logits_from_input_dict(self, q_dicts, bsize=32, progress_bar=True):
        self.model.eval()
        result_logits = []
        iter_count = (len(q_dicts) - 1) // bsize + 1
        ranger = range(iter_count) if not progress_bar else tqdm.trange(iter_count)
        for i in ranger:
            l = self.get_logits_from_input_dict_(q_dicts[i*bsize:(i+1) * bsize]).detach().cpu().numpy().tolist()
            result_logits.extend(l)
        return np.array(result_logits)

    def save(self, path):
        torch.save(self.model.state_dict(), path)

    def load(self, path):
        self.model.load_state_dict(torch.load(path))


def resize(sent_A, sent_B, max_length):
    combined_cap = max_length - 30
    toks_A = t5tok(sent_A)['input_ids']
    toks_B = t5tok(sent_B)['input_ids']
    
    toks_A_new, toks_B_new = [], []
    total_token_count = 0
    for i in range(max(len(toks_A), len(toks_B)) - 1):
        if total_token_count >= combined_cap:
            break
        if i < len(toks_A) - 1:
            toks_A_new.append(toks_A[i])
            total_token_count += 1
        
        if total_token_count >= combined_cap:
            break
        if i < len(toks_B) - 1:
            toks_B_new.append(toks_B[i])
            total_token_count += 1
    new_A, new_B = t5tok.decode(toks_A_new), t5tok.decode(toks_B_new)
    return new_A, new_B

def query_paired_fitness_controlled_(h, pos, neg, num_examples, m, max_length=128):
    q = 'Is it true that compared to sentence B, sentence A ' + h + '?'
    
    pairs = []
    for i in range(num_examples):
        sent_A = random.choice(pos)
        sent_B = random.choice(neg)
        pairs.append((sent_A, sent_B))

    qc_dicts = []
    for sent_A, sent_B in pairs:
        sent_A, sent_B = resize(sent_A, sent_B, max_length)
        c = 'sentence A: ' + sent_A + '\n\nsentence B: ' + sent_B
        qc_dicts.append({'q': q, 'c': c})
    positive_logits = m.get_logits_from_input_dict(qc_dicts, bsize=BSIZE)
    pos_score = np.mean((np.e ** positive_logits[:,1]) > 0.5)

    qc_dicts = []

    for sent_A, sent_B in pairs:
        sent_A, sent_B = resize(sent_A, sent_B, max_length)
        c = 'sentence A: ' + sent_B + '\n\nsentence B: ' + sent_A
        qc_dicts.append({'q': q, 'c': c})
    reverse_logits = m.get_logits_from_input_dict(qc_dicts, bsize=BSIZE)
    reverse_score = np.mean((np.e ** reverse_logits[:,1]) > 0.5)
    return {
        'h_score': pos_score - reverse_score,
        'h': h,
        'dicts': pairs,
        'logits': {
            'positive_logits': positive_logits,
            'reverse_logits': reverse_logits
        }
    }
    
def query_single_fitness_controlled_(h, pos, neg, num_examples, m):
    q = 'Is it true that this sentence ' + h + '?'
    pos, neg = list(pos), list(neg)
    random.shuffle(pos)
    random.shuffle(neg)
    
    pos_examples = pos[:num_examples]
    qc_dicts = [{'q': q, 'c': s} for s in pos_examples]
    pos_logits = m.get_logits_from_input_dict(qc_dicts, bsize=BSIZE)[:,1]
    
    neg_examples =  neg[:num_examples]
    qc_dicts = [{'q': q, 'c': s} for s in neg_examples]
    neg_logits = m.get_logits_from_input_dict(qc_dicts, bsize=BSIZE)[:,1]
    
    pos_score = np.mean((np.e ** pos_logits) > 0.5)
    neg_score = np.mean((np.e ** neg_logits) > 0.5)
    
    return {
        'h_score': pos_score - neg_score,
        'h': h,
        'dicts': (pos_examples, neg_examples),
        'logits': {
            'pos_logits': pos_logits,
            'neg_logits': neg_logits
        }
    }

def split_paired_fitness_controlled_(h, pos, neg, m):
    """Use iterative, active sampling with precomputed confidence to split samples."""

    CONFIDENCE = 95
    MAX_DEPTH = 18
    p_file = open(f'sampling/prefixes_{CONFIDENCE}.json', 'r')
    prefixes = json.loads(p_file.read())

    q = 'Is it true that compared to sentence B, sentence A ' + h + '?'
    
    def split(samples, max_length=128):
        """Splits the samples into the top and bottom halves."""
        np.random.shuffle(samples) # shuffle samples
        samples_a, samples_b = samples[::2], samples[1::2]

        reg_qc_dicts, rev_qc_dicts = [], []
        for sent_A, sent_B in zip_longest(samples_a, samples_b):
            if not sent_B: # in case odd
                sent_B = samples[0] # pick random sample
            resize(sent_A, sent_B, max_length)
            reg_c = 'sentence A: ' + sent_A + '\n\nsentence B: ' + sent_B
            reg_qc_dicts.append({'q': q, 'c': reg_c})
            rev_c = 'sentence A: ' + sent_B + '\n\nsentence B: ' + sent_A
            rev_qc_dicts.append({'q': q, 'c': rev_c})
        
        reg_logits = m.get_logits_from_input_dict(reg_qc_dicts, bsize=BSIZE)
        rev_logits = m.get_logits_from_input_dict(rev_qc_dicts, bsize=BSIZE)

        net_logits = reg_logits[:,1] - rev_logits[:,1]
        comparisons = (np.e ** net_logits) > 0.5

        top, bottom = [], []
        for sent_A, sent_B, comp in zip_longest(samples_a, samples_b, comparisons):
            if not sent_B:
                top.append(sent_A) if comp else bottom.append(sent_A)
            else:
                if comp: top.append(sent_A), bottom.append(sent_B)
                else: bottom.append(sent_A), top.append(sent_B)

        return np.array(top), np.array(bottom)

    dists = dict()
    dists[0] = {'':pos + neg}

    top_half = []
    bottom_half = []

    for depth in range(1, MAX_DEPTH+1):
        dists[depth] = {}
        for record, dist_to_split in dists[depth-1].items():
            
            # if reached precomputed stopping point
            if record in prefixes:
                prop = prefixes[record]
                if prop < 0.5: bottom_half.extend(dist_to_split)
                else: top_half.extend(dist_to_split)
                continue
            
            # if reached max depth
            if depth == MAX_DEPTH or len(dist_to_split) < 2:
                wins = sum(int(x) for x in record)
                if wins / len(record) >= 0.5: bottom_half.extend(dist_to_split)
                else: top_half.extend(dist_to_split)
                continue
            
            # else split and continue
            top, bottom = split(dist_to_split)
            dists[depth][record + '0'] = bottom
            dists[depth][record + '1'] = top

    pos_pos = [s for s in top_half if s in pos]
    pos_neg = [s for s in bottom_half if s in pos]
    neg_pos = [s for s in top_half if s in neg]
    neg_neg = [s for s in bottom_half if s in neg]

    return {
        'h': h,
        'pos_pos':pos_pos,
        'pos_neg':pos_neg,
        'neg_pos':neg_pos,
        'neg_neg':neg_neg,
    }

def split_single_fitness_controlled_(h, pos, neg, m):
    q = 'Is it true that this sentence ' + h + '?'
    pos = list(pos)
    neg = list(neg)

    qc_dicts = [{'q': q, 'c': s} for s in pos]
    logits = m.get_logits_from_input_dict(qc_dicts, bsize=BSIZE)[:,1]
    
    pos_pos = np.array(pos)[((np.e ** logits) > 0.5).astype(int)]
    pos_neg = np.array(pos)[((np.e ** logits) <= 0.5).astype(int)]

    qc_dicts = [{'q': q, 'c': s} for s in neg]
    logits = m.get_logits_from_input_dict(qc_dicts, bsize=BSIZE)[:,1]
    
    neg_pos = np.array(neg)[((np.e ** logits) > 0.5).astype(int)]
    neg_neg = np.array(neg)[((np.e ** logits) <= 0.5).astype(int)]

    return {
        'h': h,
        'pos_pos':pos_pos.tolist(),
        'pos_neg':pos_neg.tolist(),
        'neg_pos':neg_pos.tolist(),
        'neg_neg':neg_neg.tolist(),
        'logits':logits,
    }

    


class DummyVerifier:

    def __init__(self):
        self.seq_length = 128
        print('loading verifier')
        self.model = T5ZeroShotClfQA('allenai/unifiedqa-t5-large', 
                                     max_seq_length=self.seq_length, half_precision=True)
        print('verifier loaded')
        self.description = 'Unifiedqa t5-large for debugging'
    
    def return_verification(self, h, pos, neg, num_examples):
        result = query_paired_fitness_controlled_(h, pos, neg, num_examples, self.model, max_length=self.seq_length)
        return result

    def return_split(self, h, pos, neg):
        result = split_paired_fitness_controlled_(h, pos, neg, self.model)
        return result

    
class Verifier0514:

    def __init__(self):
        self.seq_length = 256
        print('loading verifier')
        self.model = T5ZeroShotClfQA('ruiqi-zhong/t5verifier_0514', 
                                     max_seq_length=self.seq_length, half_precision=True)
        print('verifier loaded')
        self.description = 'Similar to Verifier 1207, though the fine-tuned on clean verification data'
    
    def return_verification(self, h, pos, neg, num_examples):
        result = query_paired_fitness_controlled_(h, pos, neg, num_examples, self.model, max_length=self.seq_length)
        return result

    def return_split(self, h, pos, neg):
        result = split_paired_fitness_controlled_(h, pos, neg, self.model)
        return result

class UnifiedQASingle:
    
    def __init__(self):
        self.seq_length = 256
        print('loading verifier')
        self.model = T5ZeroShotClfQA('allenai/unifiedqa-t5-11b', 
                                     max_seq_length=self.seq_length, half_precision=True)
        self.model.eval()
        print('verifier loaded')
        self.description = 'UnifiedQA evaluated on single hypotheses'
    
    def return_verification(self, h, pos, neg, num_examples):
        result = query_single_fitness_controlled_(h, pos, neg, num_examples, self.model)
        return result
    
    def return_split(self, h, pos, neg):
        result = split_paired_fitness_controlled_(h, pos, neg, self.model)
        return result

class UnifiedQA_v2Single:
    
    def __init__(self):
        self.seq_length = 256
        print('loading verifier')
        self.model = T5ZeroShotClfQA('allenai/unifiedqa-v2-t5-11b-1251000',
                                     max_seq_length=self.seq_length, half_precision=True)
        self.model.eval()
        print('verifier loaded')
        self.description = 'UnifiedQA-v2 evaluated on single hypotheses'
    
    def return_verification(self, h, pos, neg, num_examples):
        result = query_single_fitness_controlled_(h, pos, neg, num_examples, self.model)
        return result
    
    def return_split(self, h, pos, neg):
        result = split_paired_fitness_controlled_(h, pos, neg, self.model)
        return result

class UnifiedQA_v2:

    def __init__(self):
        self.seq_length = 256
        print('loading verifier')
        self.model = T5ZeroShotClfQA('allenai/unifiedqa-v2-t5-11b-1251000',
                                     max_seq_length=self.seq_length, half_precision=True)
        self.model.eval()
        print('verifier loaded')
        self.description = 'UnifiedQA-v2 evaluated on comparison hypotheses'
    
    def return_verification(self, h, pos, neg, num_examples):
        result = query_paired_fitness_controlled_(h, pos, neg, num_examples, self.model, max_length=self.seq_length)
        return result

    def return_split(self, h, pos, neg):
        result = split_paired_fitness_controlled_(h, pos, neg, self.model)
        return result

def init_verifier(verifier_name):
    return name2verifier_cls[verifier_name]()

    
name2verifier_cls = {
    'ruiqi-zhong/t5verifier_0514': Verifier0514,
    'dummy': DummyVerifier,
    'unifiedqasingle': UnifiedQASingle,
    'unifiedqa_v2single': UnifiedQA_v2Single,
    'unifiedqa_v2': UnifiedQA_v2
}


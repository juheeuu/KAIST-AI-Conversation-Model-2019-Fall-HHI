import torch
import torch.nn as nn
from utils import to_var, pad
import layers
from utils import to_var, SOS_ID, UNK_ID, EOS_ID, PAD_ID
import torch.nn.functional as F
import torch.nn as nn
import math 

class Conv1D(nn.Module):
    def __init__(self, nf, nx):
        """ Conv1D layer as defined by Radford et al. for OpenAI GPT (and also used in GPT-2)
            Basically works like a Linear layer but the weights are transposed
        """
        super().__init__()
        self.nf = nf
        w = torch.empty(nx, nf)
        nn.init.normal_(w, std=0.02)
        self.weight = nn.Parameter(w)
        self.bias = nn.Parameter(torch.zeros(nf))

    def forward(self, x):
        size_out = x.size()[:-1] + (self.nf,)
        x = torch.addmm(self.bias, x.view(-1, x.size(-1)), self.weight)
        x = x.view(*size_out)
        return x

def gelu(x):
    return 0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))

class MultiheadAttention(nn.Module):
    def __init__(self, n_features, n_heads, attn_dropout, ff_dropout, max_seq_len):
        super(MultiheadAttention, self).__init__()
        assert n_features % n_heads == 0    
        self.register_buffer("bias", torch.tril(torch.ones(max_seq_len, max_seq_len)).view(1, 1, max_seq_len, max_seq_len))
        self.n_features = n_features
        self.n_head = n_heads

        self.c_attn = Conv1D(n_features * 3, n_features)
        self.c_proj = Conv1D(n_features, n_features)
        self.attn_dropout = nn.Dropout(attn_dropout)
        self.resid_dropout = nn.Dropout(ff_dropout)

        self._init_weights()

    def _init_weights(self):
        nn.init.normal_(self.c_attn.weight, std=0.02)
        nn.init.normal_(self.c_proj.weight, std=0.02)
    
    def _attn(self, q, k, v, attention_mask=None, head_mask=None, masked=True):
        w = torch.matmul(q, k) / math.sqrt(v.size(-1))
        nd, ns = w.size(-2), w.size(-1)
        if masked:
            b = self.bias[:, :, ns - nd : ns, :ns]
            w = w * b - 1e4 * (1 - b)

        if attention_mask is not None:
            w = w + attention_mask

        w = nn.Softmax(dim=-1)(w)
        w = self.attn_dropout(w)

        # Mask heads if we want to
        if head_mask is not None:
            w = w * head_mask

        outputs = torch.matmul(w, v)
        
        return outputs

    def merge_heads(self, x):
        x = x.permute(0, 2, 1, 3).contiguous()
        new_x_shape = x.size()[:-2] + (x.size(-2) * x.size(-1),)
        return x.view(*new_x_shape)

    def split_heads(self, x, k=False):
        new_x_shape = x.size()[:-1] + (self.n_head, x.size(-1) // self.n_head)
        # (batch, seq_len, n_head, head_features) 
        x = x.view(*new_x_shape)  # in Tensorflow implem: fct split_states
        if k:
            return x.permute(0, 2, 3, 1)  # (batch, head, head_features, seq_length)
        else:
            return x.permute(0, 2, 1, 3)  # (batch, head, seq_length, head_features)
    
    def forward(self, query, key, value, attn_mask=None, head_mask=None, qkv_same=True):
        if qkv_same:
            query, key, value = self.c_attn(query).split(self.n_features, dim=-1)
            apply_future_mask = True 
        else:
            # Calculate query key value respectively
            size_out = query.size()[:-1] + (self.n_features,)
            q_w, q_b = self.c_attn.weight[:, :self.n_features], self.c_attn.bias[:self.n_features]
            query = torch.addmm(q_b, query.view(-1, query.size(-1)), q_w).view(size_out)

            size_out = key.size()[:-1] + (self.n_features,)
            k_w, k_b = self.c_attn.weight[:, self.n_features:self.n_features * 2], self.c_attn.bias[self.n_features:self.n_features * 2]
            key = torch.addmm(k_b, key.view(-1, key.size(-1)), k_w).view(size_out)

            size_out = value.size()[:-1] + (self.n_features,)
            v_w, v_b = self.c_attn.weight[:, self.n_features * 2:], self.c_attn.bias[self.n_features * 2:]
            value = torch.addmm(v_b, value.view(-1, value.size(-1)), v_w).view(size_out)
            apply_future_mask = False

        query = self.split_heads(query)
        key = self.split_heads(key, k=True)
        value = self.split_heads(value)

        a = self._attn(query, key, value, attn_mask, head_mask, masked=apply_future_mask)

        a = self.merge_heads(a)
        a = self.c_proj(a)
        a = self.resid_dropout(a)

        return a

class MLP(nn.Module):
    def __init__(self, n_state, n_features, dropout):  # in MLP: n_state=3072 (4 * n_embd)
        super().__init__()
        self.c_fc = Conv1D(n_state, n_features)
        self.c_proj = Conv1D(n_features, n_state)
        self.act = gelu
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        h = self.act(self.c_fc(x))
        h2 = self.c_proj(h)
        return self.dropout(h2)

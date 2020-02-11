import numpy as np
import torch
import torch.nn as nn
from layers import masked_cross_entropy
from utils import to_var, PAD_ID, get_linear_schedule_with_warmup, EOS_ID, SOS_ID
import os
from tqdm import tqdm
from math import isnan
import codecs
import sys
from .solver import Solver
import torch.nn.functional as F
from subprocess import call

class SolverTransformer(Solver):
    def __init__(self, config, train_data_loader, eval_data_loader, vocab, is_train=True, model=None):
        super(SolverPTB, self).__init__(config, train_data_loader, eval_data_loader, vocab, is_train, model)
    
    def train(self):
        epoch_loss_history = list()
        min_validation_loss = sys.float_info.max
        patience_cnt = self.config.patience

        self.config.n_gpu = torch.cuda.device_count()

        t_total = len(self.train_data_loader) * self.config.n_epoch
        cur_step = 0

        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': self.config.weight_decay},
            {'params': [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        self.optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=self.config.learning_rate)
        self.scheduler = get_linear_schedule_with_warmup(
            self.optimizer, num_warmup_steps=self.config.warmup_steps, num_training_steps=t_total
        )

        if self.config.n_gpu > 1:
            self.model = torch.nn.DataParallel(self.model, device_ids=[0, 1]).to(self.config.device)
        else:
            self.model = self.model.to(self.config.device)

        for epoch_i in range(self.epoch_i, self.config.n_epoch):
            self.epoch_i = epoch_i 
            batch_loss_history = list()
            self.model.train()
            n_total_words = 0
            for batch_i, (input_utterances,
                          input_utterances_mask,
                          target_utterance,
                          target_utterance_mask,
                          ground_truth_target_utterance) in enumerate(tqdm(self.train_data_loader, ncols=80)):
    
                input_utterances = torch.LongTensor(input_utterances).to(self.config.device)
                input_utterances_mask = torch.BoolTensor(input_utterances_mask).to(self.config.device)
                target_utterance = torch.LongTensor(target_utterance).to(self.config.device)
                target_utterance_mask = torch.BoolTensor(target_utterance_mask).to(self.config.device)
                ground_truth_target_utterance = torch.LongTensor(ground_truth_target_utterance).to(self.config.device)

                self.optimizer.zero_grad()
                self.model.zero_grad()

                utterance_logits = self.model(
                    input_utterances, 
                    target_utterance
                )

                # masked cross entropy 
                loss_fn = torch.nn.CrossEntropyLoss(ignore_index=PAD_ID)
                active_loss = target_utterance_mask.view(-1) != True 
                utterance_logits = utterance_logits.contiguous().view(-1, utterance_logits.size(-1))
                ground_truth_target_utterance = ground_truth_target_utterance.view(-1)
                batch_loss = loss_fn(utterance_logits, ground_truth_target_utterance)
                target_utterance = target_utterance.view(-1)[active_loss]

                n_words = target_utterance.size(0) - 1

                assert not isnan(batch_loss.item())
                batch_loss_history.append(batch_loss.item() * n_words)
                n_total_words += n_words

                if self.config.n_gpu > 1: 
                    batch_loss = batch_loss.mean()

                if batch_i % self.config.print_every == 0:
                    tqdm.write(f'Epoch: {epoch_i+1}, iter {batch_i}: loss = {batch_loss.item():.3f}')
                    self.writer.add_scalar('batch_train_loss', batch_loss.item(), cur_step)
                    self.writer.add_scalar('learning_rate', self.scheduler.get_lr()[0], cur_step)


                batch_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.clip)
                self.optimizer.step()
                self.scheduler.step()
                cur_step += 1


            epoch_loss = np.sum(batch_loss_history) / n_total_words
            epoch_loss_history.append(epoch_loss)
            self.epoch_loss = epoch_loss

            print(f'Epoch {epoch_i+1} loss average: {epoch_loss:.3f}')

            print('\n<Validation>...')
            self.validation_loss = self.evaluate()

            if epoch_i % self.config.plot_every_epoch == 0:
                self.write_summary(epoch_i)

            if min_validation_loss > self.validation_loss:
                min_validation_loss = self.validation_loss
            else:
                patience_cnt -= 1
                self.save_model(epoch_i)

            if patience_cnt < 0:
                print(f'\nEarly stop at {epoch_i}')
                self.save_model(epoch_i)
                return epoch_loss_history

        self.save_model(self.config.n_epoch)

        return epoch_loss_history

    def evaluate(self):
        self.model.eval()
        batch_loss_history = []
        n_total_words = 0

        for batch_i, (input_utterances,
                      input_utterances_mask,
                      target_utterance,
                      target_utterance_mask,
                      ground_truth_target_utterance) in enumerate(tqdm(self.eval_data_loader, ncols=80)):
                
            with torch.no_grad():
                input_utterances = torch.LongTensor(input_utterances).to(self.config.device)
                input_utterances_mask = torch.BoolTensor(input_utterances_mask).to(self.config.device)
                target_utterance = torch.LongTensor(target_utterance).to(self.config.device)
                target_utterance_mask = torch.BoolTensor(target_utterance_mask).to(self.config.device)
                ground_truth_target_utterance = torch.LongTensor(ground_truth_target_utterance).to(self.config.device)

            utterance_logits = self.model(
                input_utterances,
                target_utterance
                )

            # masked cross entropy 
            loss_fn = torch.nn.CrossEntropyLoss(ignore_index=PAD_ID)
            active_loss = target_utterance_mask.view(-1) != True 
            utterance_logits = utterance_logits.contiguous().view(-1, utterance_logits.size(2))
            ground_truth_target_utterance = ground_truth_target_utterance.view(-1)
            batch_loss = loss_fn(utterance_logits, ground_truth_target_utterance)
            target_utterance = target_utterance.view(-1)[active_loss]
            n_words = target_utterance.size(0) - 1

            if self.config.n_gpu > 1:
                batch_loss = batch_loss.mean()

            assert not isnan(batch_loss.item())
            batch_loss_history.append(batch_loss.item() * n_words)
            n_total_words += n_words
        
        epoch_loss = np.sum(batch_loss_history) / n_total_words
        print(f'Validation loss: {epoch_loss:.3f}\n')

        return epoch_loss

    def export_samples(self, beam_size=4):
        self.model.config.beam_size = beam_size
        self.model.eval()
        n_sample_step = self.config.n_sample_step
        context_history = list()
        sample_history = list()
        ground_truth_history = list()
        generated_history = list()
        input_history = list()


        # Only support batch size 1... 
        # TODO: Extend batch size 

        for batch_i, (input_utterances,
                      input_utterances_mask,
                      _,
                      _,
                      ground_truth_target_utterance) in enumerate(tqdm(self.eval_data_loader, ncols=80)):

            context_history.append(input_utterances)
            with torch.no_grad():
                input_utterances = torch.LongTensor(input_utterances).to(self.config.device)
                input_utterances_mask = torch.BoolTensor(input_utterances_mask).to(self.config.device)

            max_seq_len =self.model.config.max_seq_len 

            enc_input = input_utterances.unsqueeze(-1)
            dec_input = torch.LongTensor([[SOS_ID]]).to(self.config.device)

            # Greedy Decoding 
            for i in range(max_seq_len):
                y_pred = self.model(input_utterances, dec_input)
                y_pred_ids = y_pred.max(dim=-1)[1]

                new_word = y_pred_ids.tolist()[0][-1]

                if new_word == EOS_ID or i == max_seq_len - 1:
                    break

                dec_input = torch.cat((dec_input, torch.LongTensor([[new_word]]).to(self.config.device)), dim=-1)
            
            labels = y_pred_ids.tolist()[0]
            labels = self.vocab.decode(labels)

            generated_history.append(labels)

            input_utterances = input_utterances.tolist()[0]
            input_utterances = self.vocab.decode(input_utterances)
            input_history.append(input_utterances)

            ground_truth = list(ground_truth_target_utterance)[0]
            ground_truth = self.vocab.decode(ground_truth)
            ground_truth_history.append(ground_truth)
        
        target_file_name = 'responses_{}_{}_{}_{}.txt'.format(self.config.mode, n_sample_step,
                                                                 beam_size, self.epoch_i)
        print("Writing candidates into file {}".format(target_file_name))
        conv_idx = 0 
        with codecs.open(os.path.join(self.config.save_path, target_file_name), 'w', "utf-8") as output_f:
            for input_utter, generated, ground_truth in tqdm(zip(input_history, generated_history, ground_truth_history)):
                print("Conversation Context {}".format(conv_idx), file=output_f)
                print(input_utter, file=output_f)
                print(generated, file=output_f)
                print(ground_truth, file=output_f)
                conv_idx += 1

        return conv_idx



import torch
import torch.nn as nn
from torch.nn import Parameter
import math
import numpy as np


class Encoder(nn.Module):
    """Maps a graph represented as an input sequence to a hidden vector
    """

    def __init__(self, input_dim, hidden_dim, use_cuda):
        super(Encoder, self).__init__()
        self.hidden_dim = hidden_dim
        self.lstm = nn.LSTM(input_dim, hidden_dim)
        self.use_cuda = use_cuda
        self.enc_init_state = self.init_hidden(hidden_dim)

    def forward(self, x, hidden):
        # hidden: (h0, c0)
        output, hidden = self.lstm(x, hidden)
        return output, hidden

    def init_hidden(self, hidden_dim):
        """Trainable initial hidden state"""
        enc_init_hx = Parameter(torch.zeros(hidden_dim), requires_grad=False)
        if self.use_cuda:
            enc_init_hx = enc_init_hx.cuda()

        # enc_init_hx.uniform_(-(1. / math.sqrt(hidden_dim)),
        #        1. / math.sqrt(hidden_dim))

        enc_init_cx = Parameter(torch.zeros(hidden_dim), requires_grad=False)
        if self.use_cuda:
            enc_init_cx = enc_init_cx.cuda()

        # enc_init_cx = nn.Parameter(enc_init_cx)
        # enc_init_cx.uniform_(-(1. / math.sqrt(hidden_dim)),
        #        1. / math.sqrt(hidden_dim))
        return enc_init_hx, enc_init_cx


class Struct2Vec(nn.Module):
    def __init__(self, node_num=21, p_dim=128, R=4):
        super(Struct2Vec, self).__init__()
        self.node_num = node_num
        self.p_dim = p_dim
        self.R = R
        self.theta_1 = nn.Linear(self.p_dim, self.p_dim, bias=False)  # mu
        self.theta_2 = nn.Linear(self.p_dim, self.p_dim, bias=False)  # ll-w
        self.theta_3 = nn.Linear(1, self.p_dim, bias=False)  # l-w

        self.theta_4 = nn.Linear(6, self.p_dim, bias=False)  # service node
        self.theta_5 = nn.Linear(2, self.p_dim, bias=False)  # depot node

    def forward(self, inputs):
        """
        :param inputs: [sourceL x batch_size x input_dim], where input_dim: 6
        :return: [sourceL x batch_size x embedded_dim]
        """
        batch_size = inputs.size(1)
        N = self.node_num
        mu = torch.zeros(N, batch_size, self.p_dim)
        mu_null = torch.zeros(N, batch_size, self.p_dim)
        for _ in range(self.R):
            for i in range(N):
                item_1 = self.theta_1(torch.sum(mu, dim=0) - mu[i])
                item_2 = self.theta_2(sum(
                    [torch.relu(self.theta_3(torch.norm(inputs[i][:, :2] - inputs[j][:, :2], dim=1, keepdim=True))) for
                     j in range(N)]))
                item_3 = self.theta_5(inputs[i][:, :2]) if i == 0 else self.theta_4(inputs[i])
                mu_null[i] = torch.relu(item_1 + item_2 + item_3)
            mu = mu_null.clone()

        return mu


class Attention(nn.Module):
    """A generic attention module for a decoder in seq2seq"""

    def __init__(self, dim, use_tanh=False, C=10, use_cuda=True):
        super(Attention, self).__init__()
        self.use_tanh = use_tanh
        self.project_query = nn.Linear(dim, dim)
        self.project_ref = nn.Conv1d(dim, dim, 1, 1)
        self.C = C  # tanh exploration
        self.tanh = nn.Tanh()

        v = torch.FloatTensor(dim)
        if use_cuda:
            v = v.cuda()
        self.v = nn.Parameter(v, requires_grad=True)
        self.v.data.uniform_(-1. / math.sqrt(dim), 1. / math.sqrt(dim))

    def forward(self, query, ref):
        """
        Args:
            query: is the hidden state of the decoder at the current time step. [batch_size x hidden_dim]
            ref: the set of hidden states from the encoder.
                [sourceL x batch_size x hidden_dim]
        """
        # ref is now [batch_size x hidden_dim x sourceL]
        ref = ref.permute(1, 2, 0)
        q = self.project_query(query).unsqueeze(2)  # [batch_size x hidden_dim x 1]
        e = self.project_ref(ref)  # [batch_size x hidden_dim x sourceL]
        # expand the query by sourceL
        # [batch x dim x sourceL]
        expanded_q = q.repeat(1, 1, e.size(2))
        # [batch x 1 x hidden_dim]
        v_view = self.v.unsqueeze(0).expand(expanded_q.size(0), len(self.v)).unsqueeze(1)
        # [batch_size x 1 x hidden_dim] * [batch_size x hidden_dim x sourceL] = [batch_size x 1 x sourceL]
        u = torch.bmm(v_view, self.tanh(expanded_q + e)).squeeze(1)
        if self.use_tanh:
            logits = self.C * self.tanh(u)
        else:
            logits = u
        return e, logits


class Decoder(nn.Module):
    def __init__(self,
                 embedding_dim,
                 hidden_dim,
                 seq_len,
                 vehicle_init_capacity,
                 s2v,
                 encoder,
                 tanh_exploration,
                 use_tanh,
                 decode_type,
                 n_glimpses=1,
                 beam_size=0,
                 use_cuda=True):
        super(Decoder, self).__init__()

        self.embedding_dim = embedding_dim + 2  # remaining capacity, current time
        self.hidden_dim = hidden_dim
        self.n_glimpses = n_glimpses
        self.seq_len = seq_len
        self.decode_type = decode_type
        self.beam_size = beam_size
        self.use_cuda = use_cuda
        self.vehicle_init_capacity = vehicle_init_capacity

        self.s2v = s2v  # structure2vector
        self.encoder = encoder
        self.decoder_lstm = nn.LSTM(input_size=self.embedding_dim, hidden_size=self.hidden_dim)
        self.pointer = Attention(hidden_dim, use_tanh=use_tanh, C=tanh_exploration, use_cuda=self.use_cuda)
        self.glimpse = Attention(hidden_dim, use_tanh=False, use_cuda=self.use_cuda)
        self.sm = nn.Softmax(dim=1)

    def apply_mask_to_logits(self, logits, mask, prev_idxs):
        if mask is None:
            mask = torch.zeros(logits.size()).long()
            if self.use_cuda:
                mask = mask.cuda()

        maskk = mask.clone()

        # to prevent them from being reselected.
        # Or, allow re-selection and penalize in the objective function
        if prev_idxs is not None:
            # set most recently selected idx values to 1
            maskk[list(range(logits.size(0))), prev_idxs] = 1  # awesome!
            maskk[torch.nonzero(prev_idxs).squeeze(1), 0] = 0  # filter
            logits[maskk] = -np.inf
        return logits, maskk

    def forward(self, decoder_input, before_embedded_inputs, embedded_inputs, hidden, context):
        """
        Args:
            decoder_input: The initial input to the decoder
                size is [batch_size x embedding_dim].
            before_embedded_inputs: [sourceL x batch_size x input_dim]
            embedded_inputs: [sourceL x batch_size x embedding_dim]
            hidden: the prev hidden state, size is [batch_size x hidden_dim].
                Initially this is set to (enc_h[-1], enc_c[-1])
            context: encoder outputs, [sourceL x batch_size x hidden_dim]
        """

        def recurrence(x, hidden, logit_mask, prev_idxs):

            output, (hy, cy) = self.decoder_lstm(x, hidden)

            g_l = hy
            for _ in range(self.n_glimpses):
                ref, logits = self.glimpse(g_l, context)
                logits, logit_mask = self.apply_mask_to_logits(logits, logit_mask, prev_idxs)
                # [batch_size x h_dim x sourceL] * [batch_size x sourceL x 1] = [batch_size x h_dim x 1]
                g_l = torch.bmm(ref, self.sm(logits).unsqueeze(2)).squeeze(2)
            _, logits = self.pointer(g_l, context)

            logits, logit_mask = self.apply_mask_to_logits(logits, logit_mask, prev_idxs)
            # if logits are all -inf, probs: [batch_size x sourceL]

            probs = self.sm(logits)
            return hy, cy, probs, logit_mask

        batch_size = context.size(1)
        sourceL = context.size(0)
        outputs = []
        selections = []
        mask = None

        idxs = torch.LongTensor([0] * batch_size)  #
        selections.append(idxs.cuda() if self.use_cuda else idxs)  #
        choose_i = torch.LongTensor([0]).cuda() if self.use_cuda else torch.LongTensor([0])
        prob_0 = torch.zeros(batch_size, sourceL).cuda() if self.use_cuda else torch.zeros(batch_size, sourceL)
        prob_0.index_fill_(1, choose_i, 1)
        outputs.append(prob_0)

        # record remaining capacity and current time
        rc_ct = torch.FloatTensor([self.vehicle_init_capacity, 0]).repeat(batch_size, 1)

        if self.decode_type == 'stochastic':
            # at most twice (seq_len - 1)
            for _ in range((self.seq_len - 1) * 2):
                hx, cx, probs, mask = recurrence(decoder_input, hidden, mask, idxs)
                # select the next inputs for the decoder [batch_size x hidden_dim]
                decoder_input, zero_idxs, idxs, rc_ct, before_embedded_inputs, embedded_inputs = self.decode_stochastic(
                    probs, before_embedded_inputs, embedded_inputs, idxs, rc_ct)

                # re-encode the embedded_inputs which are modified
                context_change, (enc_h_t_change, enc_c_t_change) = self.encoder(embedded_inputs[:, zero_idxs, :])

                # update context, hidden
                context[:, zero_idxs, :] = context_change
                hx[zero_idxs, :] = enc_h_t_change[-1]
                cx[zero_idxs, :] = enc_c_t_change[-1]
                hidden = (hx, cx)

                # use outs to point to next object
                outputs.append(probs)
                selections.append(idxs)

            return (outputs, selections), hidden

        elif self.decode_type == 'greedy':
            # embedded_inputs: [sourceL x batch_size x embedding_dim]
            # decoder_input: [batch_size x embedding_dim]
            # hidden: [batch_size x hidden_dim]
            # context: [sourceL x batch_size x hidden_dim]
            pass

    def decode_stochastic(self, probs, before_embedded_inputs, embedded_inputs, prev_idxs, rc_ct):
        """
        Return the next input for the decoder by selecting the
        input corresponding to the max output

        Args:
            probs: [batch_size x sourceL]
            before_embedded_inputs: [sourceL x batch_size x input_dim]
            embedded_inputs: [sourceL x batch_size x embedding_dim]
            # selections: list of all of the previously selected indices during decoding
       Returns:
            Tensor of size [batch_size x sourceL] containing the embeddings
            from the inputs corresponding to the [batch_size] indices
            selected for this iteration of the decoding, as well as the
            corresponding indicies
        """
        batch_size = probs.size(0)
        # idxs is [batch_size]
        idxs = probs.multinomial(1).squeeze(1)  # if data is all 0

        # nonzero
        nonzero_idxs = torch.nonzero(idxs).squeeze(1)
        before_embedded_inputs[idxs[nonzero_idxs], nonzero_idxs, -1] = 1  # convert h to 1

        # zero
        zero_idxs = torch.from_numpy(np.where(idxs == 0)[0])
        if zero_idxs:
            re_before_embedded_inputs = before_embedded_inputs[0, zero_idxs, :]  # this part needs to re-embed
            embedded_inputs[0, zero_idxs, :] = self.s2v(re_before_embedded_inputs)

        # remaining capacity, current time
        # if vehicle returns to depot 0, set remaining capacity to vehicle_init_capacity, current time to 0
        sels = torch.zeros(batch_size, self.embedding_dim)
        sels[:, :-2] = embedded_inputs[idxs, list(range(batch_size)), :]  # [batch_size x embedding_size]

        prev_x_y = before_embedded_inputs[prev_idxs, list(range(batch_size)), :2]
        cur_x_y = before_embedded_inputs[idxs, list(range(batch_size)), :2]
        distance = torch.norm(prev_x_y - cur_x_y, dim=1)

        # remaining capacity
        required_capacity = before_embedded_inputs[idxs, list(range(batch_size)), 2]
        t_1 = before_embedded_inputs[idxs, list(range(batch_size)), 3]

        # current time
        for i in range(batch_size):
            if i in nonzero_idxs:
                cur_t = rc_ct[i][1] + distance[i]
                if cur_t < t_1[i]:
                    rc_ct[i][1] = t_1[i]
                else:
                    rc_ct[i][1] = cur_t

                rc_ct[0] = rc_ct[0] - required_capacity[i]

        rc_ct[zero_idxs, :] = torch.FloatTensor([self.vehicle_init_capacity, 0])

        sels[:, -2:] = rc_ct.clone()

        return sels, zero_idxs, idxs, rc_ct, before_embedded_inputs, embedded_inputs


class PointerNetwork(nn.Module):
    """The pointer network, which is the core seq2seq model
    """

    def __init__(self,
                 embedding_dim,
                 hidden_dim,
                 seq_len,
                 n_glimpses,
                 tanh_exploration,
                 use_tanh,
                 beam_size,
                 use_cuda,
                 vehicle_init_capacity,
                 p_dim,
                 R):
        super(PointerNetwork, self).__init__()

        self.use_cuda = use_cuda
        self.vehicle_init_capacity = vehicle_init_capacity
        self.embedding_dim = embedding_dim

        self.Struct2Vec = Struct2Vec(seq_len, p_dim, R)

        self.encoder = Encoder(
            embedding_dim,
            hidden_dim,
            use_cuda)

        self.decoder = Decoder(
            embedding_dim,
            hidden_dim,
            seq_len,
            vehicle_init_capacity=self.vehicle_init_capacity,
            s2v=self.Struct2Vec,
            encoder=self.encoder,
            tanh_exploration=tanh_exploration,
            use_tanh=use_tanh,
            decode_type='stochastic',
            n_glimpses=n_glimpses,
            beam_size=beam_size,
            use_cuda=use_cuda)

        # Trainable initial hidden states
        # dec_in_0 = torch.FloatTensor(embedding_dim)
        # if use_cuda:
        #     dec_in_0 = dec_in_0.cuda()
        #
        # self.decoder_in_0 = nn.Parameter(dec_in_0)
        # self.decoder_in_0.data.uniform_(-1. / math.sqrt(embedding_dim), 1. / math.sqrt(embedding_dim))

    def forward(self, inputs):
        """ Propagate inputs through the network
        Args:
            inputs: [batch_size x sourceL x input_dim]
        """
        # embedded_inputs: [sourceL x batch_size x embedding_dim]
        embedded_inputs = self.Struct2Vec(inputs.permute(1, 0, 2))

        (encoder_hx, encoder_cx) = self.encoder.enc_init_state
        encoder_hx = encoder_hx.unsqueeze(0).repeat(embedded_inputs.size(1), 1).unsqueeze(
            0)  # [1 x batch_size x hidden_dim]
        encoder_cx = encoder_cx.unsqueeze(0).repeat(embedded_inputs.size(1), 1).unsqueeze(0)

        # encoder forward pass
        # context: [seq_len x batch_size x hidden_dim], enc_h_t: [1 x batch_size x hidden_dim]
        context, (enc_h_t, enc_c_t) = self.encoder(embedded_inputs, (encoder_hx, encoder_cx))

        dec_init_state = (enc_h_t[-1], enc_c_t[-1])

        # decoder_input: [batch_size x embedding_dim]
        decoder_input = torch.zeros(embedded_inputs.size(1), self.embedding_dim + 2)  # remaining capacity, current time
        decoder_input[:, -2] = self.vehicle_init_capacity
        decoder_input[:, :-2] = embedded_inputs[0].clone()
        decoder_input.detach_()
        if self.use_cuda:
            decoder_input = decoder_input.cuda()
        (pointer_probs, input_idxs), dec_hidden_t = self.decoder(decoder_input,
                                                                 inputs,  # before_embedded_inputs
                                                                 embedded_inputs,
                                                                 dec_init_state,
                                                                 context)

        return pointer_probs, input_idxs


class CriticNetwork(nn.Module):
    """Useful as a baseline in REINFORCE updates"""

    def __init__(self,
                 embedding_dim,
                 hidden_dim,
                 n_process_blocks,
                 tanh_exploration,
                 use_tanh,
                 use_cuda):
        super(CriticNetwork, self).__init__()

        self.hidden_dim = hidden_dim
        self.n_process_blocks = n_process_blocks

        self.encoder = Encoder(embedding_dim,
                               hidden_dim,
                               use_cuda)

        self.process_block = Attention(hidden_dim,
                                       use_tanh=use_tanh,
                                       C=tanh_exploration,
                                       use_cuda=use_cuda)
        self.sm = nn.Softmax(dim=1)
        self.decoder = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1)  # baseline prediction, a single scalar
        )

    def forward(self, inputs):
        """
        Args:
            inputs: [sourceL x batch_size x embedding_dim] of embedded inputs
        """

        (encoder_hx, encoder_cx) = self.encoder.enc_init_state  # [hidden_dim]
        encoder_hx = encoder_hx.unsqueeze(0).repeat(inputs.size(1), 1).unsqueeze(0)  # [1 x batch_size x hidden_dim]
        encoder_cx = encoder_cx.unsqueeze(0).repeat(inputs.size(1), 1).unsqueeze(0)

        # encoder forward pass
        enc_outputs, (enc_h_t, enc_c_t) = self.encoder(inputs, (encoder_hx, encoder_cx))

        # grab the hidden state and process it via the process block
        process_block_state = enc_h_t[-1]  # [batch_size x hidden_dim]
        for _ in range(self.n_process_blocks):
            ref, logits = self.process_block(process_block_state, enc_outputs)
            process_block_state = torch.bmm(ref, self.sm(logits).unsqueeze(2)).squeeze(2)
        # produce the final scalar output
        out = self.decoder(process_block_state)
        return out


class NeuralCombOptRL(nn.Module):
    """
    This module contains the PointerNetwork (actor) and CriticNetwork (critic).
    It requires an application-specific reward function
    """

    def __init__(self,
                 embedding_dim,
                 hidden_dim,
                 seq_len,
                 n_glimpses,
                 n_process_blocks,
                 tanh_exploration,  # C
                 use_tanh,
                 beam_size,
                 objective_fn,  # reward function
                 is_train,
                 use_cuda,
                 vehicle_init_capacity,
                 p_dim=128,
                 R=4):
        super(NeuralCombOptRL, self).__init__()
        self.objective_fn = objective_fn
        self.is_train = is_train
        self.use_cuda = use_cuda

        self.actor_net = PointerNetwork(
            embedding_dim,
            hidden_dim,
            seq_len,
            n_glimpses,
            tanh_exploration,
            use_tanh,
            beam_size,
            use_cuda,
            vehicle_init_capacity,
            p_dim,
            R)

        # utilize critic network
        self.critic_net = CriticNetwork(
            embedding_dim,
            hidden_dim,
            n_process_blocks,
            tanh_exploration,
            False,
            use_cuda)

    def forward(self, inputs):
        """
        Args:
            inputs: [batch_size, sourceL, input_dim]
        """
        batch_size = inputs.size(0)

        # query the actor net for the input indices
        # making up the output, and the pointer attn
        probs_, action_idxs = self.actor_net(inputs)
        # probs_: [seq_len x batch_size x seq_len], action_idxs: [seq_len x batch_size]

        # Select the actions (inputs pointed to by the pointer net)
        actions = []

        for action_id in action_idxs:
            actions.append(inputs[list(range(batch_size)), action_id, :])

        if self.is_train:
            # probs_ is a list of len sourceL of [batch_size x sourceL]
            # probs: [sourceL x batch_size]
            probs = []
            for prob, action_id in zip(probs_, action_idxs):
                probs.append(prob[list(range(batch_size)), action_id])
        else:
            # return the list of len sourceL of [batch_size x sourceL]
            probs = probs_

        # get the critic value fn estimates for the baseline
        # [batch_size]
        b = self.critic_net(inputs)

        action_idxs = torch.cat(action_idxs, 0).view(-1, batch_size).transpose(1, 0).tolist()

        # return R, b, probs, actions, action_idxs
        return b, probs, actions, action_idxs
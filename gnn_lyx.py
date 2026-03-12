import torch
import torch.nn as nn
import torch.nn.functional as F

import os


class SingerHeadGatLayer(nn.Module):
    def __init__(self, in_dim_A, in_dim_B, out_dim):
        super(SingerHeadGatLayer, self).__init__()
        self.in_dim_A = in_dim_A
        self.in_dim_B = in_dim_B
        self.out_dim = out_dim

        self.WA = nn.Linear(in_dim_A, out_dim)
        self.WB = nn.Linear(in_dim_B, out_dim)
        self.a_A = nn.Linear(out_dim, 1)
        self.a_B = nn.Linear(out_dim, 1)
        self.leakyReLU = nn.LeakyReLU(0.01)

    def forward(
        self, h_A, h_B, mask
    ):  # h_A: [batch_size, num_A, in_dim_A], h_B: [batch_size, num_B, in_dim_B], mask: [batch_size, num_A, num_B]
        z_A = self.WA(h_A)  # z_A: [batch_size, num_A, out_dim]
        z_B = self.WB(h_B)  # z_B: [batch_size, num_B, out_dim]

        e_ij = self.a_A(z_A) + self.a_B(z_B).squeeze(-1).unsqueeze(
            -2
        )  # e_ij: [batch_size, num_A, num_B]
        e_ij_masked = e_ij.masked_fill_(~mask, float(-1e30))  # 掩码处理

        alpha_ij = F.softmax(
            self.leakyReLU(e_ij_masked), dim=-1
        )  # alpha_ij: [batch_size, num_A, num_B]
        alpha_ij_masked = torch.where(
            mask, alpha_ij, torch.zeros_like(alpha_ij).detach()
        )  # 防止除0变nan

        h_A_out = torch.bmm(
            alpha_ij_masked, z_B
        )  # h_A_out: [batch_size, num_A, out_dim]
        return h_A_out


class MultiHeadGatLayer(nn.Module):
    def __init__(self, in_dim_A, in_dim_B, out_dim, num_head):
        super(MultiHeadGatLayer, self).__init__()
        self.in_dim_A = in_dim_A
        self.in_dim_B = in_dim_B
        self.out_dim = out_dim
        self.num_head = num_head
        self.out_dim_per_head = out_dim // num_head

        # 多头注意力
        self.head_list = nn.ModuleList(
            [
                SingerHeadGatLayer(in_dim_A, in_dim_B, self.out_dim_per_head)
                for _ in range(num_head)
            ]
        )

        # 再加个线性投影层
        self.W = nn.Linear(out_dim, out_dim)

    def forward(self, h_A, h_B, mask):
        h_A_out = [
            head_layer(h_A, h_B, mask) for head_layer in self.head_list
        ]  # 说是torch实际处理会当作并行
        return self.W(torch.cat(h_A_out, dim=-1))


class gnn_lyx(nn.Module):
    def __init__(self, hparams):
        super(gnn_lyx, self).__init__()
        self.hparams = hparams

        # 特征嵌入层
        self.link_embed = nn.Linear(4, hparams['link_state_dim'])
        self.path_embed = nn.Linear(1, hparams['path_state_dim'])

        # 状态更新层
        self.path_update = nn.GRUCell(
            input_size=hparams['path_state_dim'], hidden_size=hparams['path_state_dim']
        )
        self.link_update = nn.GRUCell(
            input_size=hparams['link_state_dim'], hidden_size=hparams['link_state_dim']
        )

        # 注意力层
        self.path_attention = MultiHeadGatLayer(
            hparams['path_state_dim'],
            hparams['link_state_dim'],
            hparams['path_state_dim'],
            hparams['head_num'],
        )
        self.link_attention = MultiHeadGatLayer(
            hparams['link_state_dim'],
            hparams['path_state_dim'],
            hparams['link_state_dim'],
            hparams['head_num'],
        )

        # 读出层
        self.readout = nn.Sequential(
            nn.Linear(hparams['path_state_dim'], hparams['readout_units']),
            nn.SELU(),
            nn.Linear(hparams['readout_units'], hparams['readout_units']),
            nn.SELU(),
            nn.Linear(hparams['readout_units'], 1),
        )

        # 用SELU要改一下初始化
        def init_weights(m):
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, nonlinearity='linear')  # 关键设置
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

        self.readout.apply(init_weights)  # 初始化

    def forward(self, features):
        link_attr = features['link_attr']
        path_attr = features['path_attr']
        mask = features['mask']

        # 1.映射层
        h_link = self.link_embed(link_attr)  # [batch_size, num_link, link_dim]
        h_path = self.path_embed(path_attr)  # [batch_size, num_path, path_dim]

        batch_size, num_link, link_dim = h_link.shape
        batch_size, num_path, path_dim = h_path.shape

        # 2.消息聚合层
        for _ in range(self.hparams['T']):
            # 2.1.路径消息聚合
            aggregated_message_path = self.path_attention(
                h_path, h_link, mask
            )  # [batch_size, num_path, path_dim]

            # 2.2.路径更新
            aggregated_message_path_flat = aggregated_message_path.reshape(
                -1, path_dim
            )  # [batch_size * num_path, path_dim]
            h_path_flat = h_path.reshape(-1, path_dim)
            next_h_path_flat = self.path_update(
                aggregated_message_path_flat, h_path_flat
            )  # [batch_size * num_path, path_dim]
            next_h_path = next_h_path_flat.reshape(batch_size, num_path, path_dim)

            # 2.3.链路消息聚合
            aggregated_message_link = self.link_attention(
                h_link, next_h_path, mask.transpose(1, 2)
            )  # [batch_size, num_link, link_dim]

            # 2.4.链路更新
            aggregated_message_link_flat = aggregated_message_link.reshape(-1, link_dim)
            h_link_flat = h_link.reshape(-1, link_dim)
            next_h_link_flat = self.link_update(
                aggregated_message_link_flat, h_link_flat
            )  # # [batch_size * num_link, link_dim]
            next_h_link = next_h_link_flat.reshape(batch_size, num_link, link_dim)

            # 2.5.更新状态
            h_link = next_h_link
            h_path = next_h_path

        # 3.读出层
        aggregated_message_path = torch.mean(h_path, dim=-2)  # [batch_size, path_dim]

        q_value = self.readout(aggregated_message_path).squeeze(-1)  # [batch_size]

        return q_value


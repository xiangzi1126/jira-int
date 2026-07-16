from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
# 引入阿里云 ECS SDK
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models

import configparser
import os
import csv
import logging
from typing import List, Dict


def init_logger():
    # 日志输出到 jira/log 目录下
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(log_dir, 'aliyun_ecs_collect.log'), encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()

# 数据目录指向 jira/data
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))


def read_config():
    config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
    config = configparser.ConfigParser()

    if not os.path.exists(config_path):
        logger.error(f"严重错误: 找不到配置文件 {config_path}")
    else:
        logger.info(f"成功加载配置文件: {config_path}")

    config.read(config_path, encoding='utf-8')
    return config


def get_valid_op_accounts() -> set:
    """从 jira_get_op_account.csv 读取需要盘点的账号列表"""
    op_account_file = os.path.join(DATA_DIR, 'jira_get_op_account.csv')
    valid_accounts = set()

    if not os.path.exists(op_account_file):
        logger.warning(f"运维账号过滤文件不存在: {op_account_file}，将不进行过滤。")
        return valid_accounts

    with open(op_account_file, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            account = row.get('资源所属账号', '').strip()
            if account:
                valid_accounts.add(account)

    logger.info(f"从 CSV 加载了 {len(valid_accounts)} 个需运维的账号白名单。")
    return valid_accounts


def get_sts_credentials(config, role_section: str):
    ak_id = config.get('aliyun', 'access_key_id')
    ak_secret = config.get('aliyun', 'access_key_secret')
    role_arn = config.get(role_section, 'role_arn')
    role_session_name = config.get(role_section, 'role_session_name')

    sts_cfg = open_api_models.Config(access_key_id=ak_id, access_key_secret=ak_secret)
    sts_cfg.endpoint = 'sts.aliyuncs.com'
    resp = StsClient(sts_cfg).assume_role(AssumeRoleRequest(
        role_arn=role_arn, role_session_name=role_session_name, duration_seconds=3600
    ))
    return resp.body.credentials, role_session_name


def create_ecs_client(creds, region_id='cn-hangzhou') -> EcsClient:
    """初始化 ECS 客户端（按地域区分）"""
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = f'ecs.{region_id}.aliyuncs.com'
    return EcsClient(cfg)


def get_all_regions(base_client: EcsClient) -> List[str]:
    """通过 API 动态获取该账号下所有可用的地域列表"""
    try:
        req = ecs_models.DescribeRegionsRequest()
        resp = base_client.describe_regions(req)
        return [r.region_id for r in resp.body.regions.region]
    except Exception as e:
        logger.error(f"获取地域列表失败: {e}，将使用常用地域进行降级。")
        return ['cn-hangzhou', 'cn-beijing', 'cn-shanghai', 'cn-shenzhen', 'cn-hongkong', 'ap-northeast-1']


def get_account_ecs_instances(creds, role_session_name: str) -> List[Dict]:
    """遍历所有地域，提取当前账号下所有的 ECS 实例详细信息（包含标签）"""
    base_client = create_ecs_client(creds, 'cn-hangzhou')
    regions = get_all_regions(base_client)

    all_instances = []

    for region in regions:
        client = create_ecs_client(creds, region)
        page_number = 1

        while True:
            req = ecs_models.DescribeInstancesRequest(
                region_id=region,
                page_size=100,
                page_number=page_number
            )

            try:
                resp = client.describe_instances(req)
                if not resp.body.instances or not resp.body.instances.instance:
                    break

                for inst in resp.body.instances.instance:
                    # 解析内网 IP
                    private_ips = []
                    if inst.vpc_attributes and inst.vpc_attributes.private_ip_address and inst.vpc_attributes.private_ip_address.ip_address:
                        private_ips = inst.vpc_attributes.private_ip_address.ip_address
                    elif inst.inner_ip_address and inst.inner_ip_address.ip_address:
                        private_ips = inst.inner_ip_address.ip_address

                    # 解析公网 IP
                    public_ips = []
                    if inst.public_ip_address and inst.public_ip_address.ip_address:
                        public_ips = inst.public_ip_address.ip_address
                    elif inst.eip_address and inst.eip_address.ip_address:
                        public_ips.append(inst.eip_address.ip_address)

                    # 解析标签 Tags
                    tags_list = []
                    if inst.tags and inst.tags.tag:
                        for tag in inst.tags.tag:
                            tags_list.append(f"{tag.tag_key}:{tag.tag_value}")
                    tags_str = " | ".join(tags_list)

                    # 基础信息组装
                    item = {
                        '资源所属账号': role_session_name,
                        'RegionId': inst.region_id,
                        'InstanceId': inst.instance_id,
                        'InstanceName': inst.instance_name,
                        'Status': inst.status,
                        'CPU(核)': inst.cpu,
                        'Memory(MB)': inst.memory,
                        'PrivateIp': ','.join(private_ips),
                        'PublicIp': ','.join(public_ips),
                        'CreationTime': inst.creation_time,
                        '标签': tags_str
                    }

                    all_instances.append(item)

                if len(resp.body.instances.instance) < 100:
                    break
                page_number += 1

            except Exception as e:
                logger.debug(f"[{role_session_name}] 获取地域 {region} 的实例失败 (可忽略): {e}")
                break

    return all_instances


def write_ecs_csv(all_instances: List[Dict]):
    """将所有账号的所有实例汇总写入到单个 CSV 文件中"""
    out_file = os.path.join(DATA_DIR, 'aliyun_ecs_op.csv')

    # 固定的基础表头
    fields = [
        '资源所属账号', 'RegionId', 'InstanceId', 'InstanceName',
        'Status', 'CPU(核)', 'Memory(MB)',
        'PrivateIp', 'PublicIp', 'CreationTime', '标签'
    ]

    with open(out_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for inst in all_instances:
            writer.writerow({k: inst.get(k, '') for k in fields})

    return out_file


def main():
    logger.info("=" * 40)
    logger.info("🚀 开始执行 阿里云全局 ECS 资源盘点任务...")

    config = read_config()
    role_sections = [s for s in config.sections() if s.startswith('aliyun-')]

    if not role_sections:
        logger.error("🛑 未在 aliyun_config.ini 中找到任何角色配置！")
        return

    valid_accounts = get_valid_op_accounts()
    all_ecs_data = []

    for role_section in role_sections:
        logger.info(f"\n{'─' * 30}\n🔍 正在处理配置段: {role_section}\n{'─' * 30}")
        try:
            target_session_name = config.get(role_section, 'role_session_name', fallback='')

            if valid_accounts and target_session_name not in valid_accounts:
                logger.info(f"  [跳过] 账号 '{target_session_name}' 不在白名单过滤列表中。")
                continue

            creds, role_session_name = get_sts_credentials(config, role_section)
            logger.info(f"  🔑 STS 授权成功，当前扮演账号: {role_session_name}，正在全地域拉取...")

            instances = get_account_ecs_instances(creds, role_session_name)

            if not instances:
                logger.warning(f"    [警告] 账号 {role_session_name} 下未找到任何 ECS 实例。")
            else:
                logger.info(f"    ✅ 账号 {role_session_name} 共盘点出 {len(instances)} 台 ECS 实例。")
                all_ecs_data.extend(instances)

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    if all_ecs_data:
        out_path = write_ecs_csv(all_ecs_data)
        logger.info(f"\n✅ 批处理完成！总计盘点出 {len(all_ecs_data)} 台 ECS 实例信息。")
        logger.info(f"💾 数据已成功汇总保存至 -> {out_path}")
    else:
        logger.warning("\n⚠️ 批处理完成，但在所有符合条件的账号中未发现任何 ECS 资源。")


if __name__ == '__main__':
    main()
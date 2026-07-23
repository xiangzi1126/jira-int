"""阿里云 RocketMQ 资源盘点 -> aliyun_mq_op.csv"""
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_ons20190214.client import Client as OnsClient
from alibabacloud_ons20190214 import models as ons_models

import configparser
import os
import csv
import logging
from typing import List, Dict


def init_logger():
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(log_dir, 'aliyun_mq_collect.log'), encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))


def read_config():
    config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
    config = configparser.ConfigParser()
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在: {config_path}")
    else:
        logger.info(f"加载配置文件: {config_path}")
    config.read(config_path, encoding='utf-8')
    return config


def get_valid_op_accounts() -> set:
    op_account_file = os.path.join(DATA_DIR, 'jira_get_op_account.csv')
    valid_accounts = set()
    if not os.path.exists(op_account_file):
        return valid_accounts
    with open(op_account_file, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            account = row.get('资源所属账号', '').strip()
            if account:
                valid_accounts.add(account)
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


def create_ons_client(creds, region_id='cn-hangzhou') -> OnsClient:
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = f'ons.{region_id}.aliyuncs.com'
    return OnsClient(cfg)


def get_account_mq_instances(creds, role_session_name: str) -> List[Dict]:
    """遍历常用地域，获取所有 RocketMQ 实例及其 ConsumerGroup"""
    regions = ['cn-hangzhou', 'cn-beijing', 'cn-shanghai', 'cn-shenzhen',
               'cn-hongkong', 'ap-northeast-1', 'ap-southeast-1']
    all_instances = []

    for region in regions:
        client = create_ons_client(creds, region)

        # 1. 获取实例列表
        try:
            req = ons_models.OnsInstanceInServiceListRequest()
            resp = client.ons_instance_in_service_list(req)
            instances = resp.body.data
            if not instances or not instances.instance_vo:
                continue
        except Exception as e:
            logger.debug(f"[{role_session_name}] 地域 {region} MQ实例查询失败(可忽略): {e}")
            continue

        for inst in instances.instance_vo:
            instance_id = inst.instance_id
            instance_name = inst.instance_name or instance_id

            # 2. 获取该实例下的 ConsumerGroup 列表
            groups = _get_consumer_groups(client, instance_id)

            if groups:
                for gid in groups:
                    all_instances.append({
                        '资源所属账号': role_session_name,
                        'RegionId': region,
                        'InstanceId': instance_id,
                        'InstanceName': instance_name,
                        'GroupId': gid,
                    })
            else:
                # 无Group也记录实例（GroupId留空）
                all_instances.append({
                    '资源所属账号': role_session_name,
                    'RegionId': region,
                    'InstanceId': instance_id,
                    'InstanceName': instance_name,
                    'GroupId': '',
                })

    return all_instances


def _get_consumer_groups(client: OnsClient, instance_id: str) -> List[str]:
    """获取指定实例下的所有 ConsumerGroup ID"""
    groups = []
    try:
        req = ons_models.OnsGroupListRequest(instance_id=instance_id)
        resp = client.ons_group_list(req)
        if resp.body.data and resp.body.data.subscribe_info_do:
            for g in resp.body.data.subscribe_info_do:
                if g.group_id:
                    groups.append(g.group_id)
    except Exception as e:
        logger.debug(f"  获取 {instance_id} 的Group列表失败: {e}")
    return groups


def write_csv(all_instances: List[Dict]):
    out_file = os.path.join(DATA_DIR, 'aliyun_mq_op.csv')
    fields = ['资源所属账号', 'RegionId', 'InstanceId', 'InstanceName', 'GroupId']
    with open(out_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for inst in all_instances:
            writer.writerow({k: inst.get(k, '') for k in fields})
    return out_file


def main():
    logger.info("=" * 40)
    logger.info("开始执行 RocketMQ 资源盘点任务...")

    config = read_config()
    role_sections = [s for s in config.sections() if s.startswith('aliyun-')]
    if not role_sections:
        logger.error("未找到任何 aliyun- 配置段")
        return

    valid_accounts = get_valid_op_accounts()
    all_data = []

    for role_section in role_sections:
        try:
            target_session = config.get(role_section, 'role_session_name', fallback='')
            if not target_session:
                continue
            if valid_accounts and target_session not in valid_accounts:
                logger.info(f"  [跳过] {target_session} 不在白名单中")
                continue

            creds, role_session_name = get_sts_credentials(config, role_section)
            logger.info(f"  STS授权成功: {role_session_name}")

            instances = get_account_mq_instances(creds, role_session_name)
            if instances:
                logger.info(f"  [{role_session_name}] 发现 {len(instances)} 条MQ记录(含Group)")
                all_data.extend(instances)
            else:
                logger.info(f"  [{role_session_name}] 无RocketMQ实例")

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    if all_data:
        out_path = write_csv(all_data)
        logger.info(f"RocketMQ盘点完成: {len(all_data)} 条记录 -> {out_path}")
    else:
        logger.warning("RocketMQ盘点完成: 未发现任何实例")


if __name__ == '__main__':
    main()

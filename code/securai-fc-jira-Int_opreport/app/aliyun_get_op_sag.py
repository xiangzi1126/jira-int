"""阿里云 SAG (智能接入网关) 资源盘点 -> aliyun_sag_op.csv"""
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_smartag20180313.client import Client as SmartagClient
from alibabacloud_smartag20180313 import models as smartag_models

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
            logging.FileHandler(os.path.join(log_dir, 'aliyun_sag_collect.log'), encoding='utf-8'),
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


def create_smartag_client(creds, region_id='cn-hangzhou') -> SmartagClient:
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = f'smartag.{region_id}.aliyuncs.com'
    return SmartagClient(cfg)


def get_account_sag_instances(creds, role_session_name: str) -> List[Dict]:
    """遍历支持的地域，获取所有 SAG 实例"""
    # SAG 仅支持部分地域（cn-hangzhou/cn-beijing 等不支持）
    regions = ['ap-northeast-1', 'cn-shanghai', 'cn-shenzhen',
               'cn-hongkong', 'ap-southeast-1', 'ap-southeast-2']
    all_instances = []

    for region in regions:
        client = create_smartag_client(creds, region)
        page_number = 1

        while True:
            req = smartag_models.DescribeSmartAccessGatewaysRequest(
                region_id=region,
                page_size=50,
                page_number=page_number
            )
            try:
                resp = client.describe_smart_access_gateways(req)
                sags = resp.body.smart_access_gateways
                if not sags or not sags.smart_access_gateway:
                    break

                for sag in sags.smart_access_gateway:
                    all_instances.append({
                        '资源所属账号': role_session_name,
                        'RegionId': region,
                        'SmartAGId': sag.smart_agid,
                        'Name': sag.name or '',
                        'Status': sag.status,
                        'CreateTime': str(sag.create_time) if sag.create_time else '',
                    })

                if len(sags.smart_access_gateway) < 50:
                    break
                page_number += 1

            except Exception as e:
                logger.debug(f"[{role_session_name}] 地域 {region} SAG查询失败(可忽略): {e}")
                break

    return all_instances


def write_csv(all_instances: List[Dict]):
    out_file = os.path.join(DATA_DIR, 'aliyun_sag_op.csv')
    fields = ['资源所属账号', 'RegionId', 'SmartAGId', 'Name', 'Status', 'CreateTime']
    with open(out_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for inst in all_instances:
            writer.writerow({k: inst.get(k, '') for k in fields})
    return out_file


def main():
    logger.info("=" * 40)
    logger.info("开始执行 SAG 资源盘点任务...")

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

            instances = get_account_sag_instances(creds, role_session_name)
            if instances:
                logger.info(f"  [{role_session_name}] 发现 {len(instances)} 个SAG实例")
                all_data.extend(instances)
            else:
                logger.info(f"  [{role_session_name}] 无SAG实例")

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    if all_data:
        out_path = write_csv(all_data)
        logger.info(f"SAG盘点完成: {len(all_data)} 个实例 -> {out_path}")
    else:
        logger.warning("SAG盘点完成: 未发现任何实例")


if __name__ == '__main__':
    main()

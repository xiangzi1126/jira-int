"""阿里云 RDS 资源盘点 -> aliyun_rds_op.csv

输出列: 资源所属账号,RegionId,DBInstanceId,DBInstanceDescription,DBInstanceStatus,
Engine,EngineVersion,DBInstanceClass,CPU(核),memory(GB),disk(GB),标签
CPU/内存/磁盘(GB)取自 DescribeDBInstanceAttribute(列表接口常缺 CPU，故以规格类 Nc 后缀兜底，如 mysql.x2.large.2c -> 2 核)；标签取自 DescribeTags。
"""
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_rds20140815.client import Client as RdsClient
from alibabacloud_rds20140815 import models as rds_models

import configparser
import os
import csv
import re
import logging
from typing import List, Dict


def init_logger():
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(log_dir, 'aliyun_rds_collect.log'), encoding='utf-8'),
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


def create_rds_client(creds, region_id='cn-hangzhou') -> RdsClient:
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = f'rds.{region_id}.aliyuncs.com'
    return RdsClient(cfg)


def _get_rds_attrs(client: RdsClient, dbinstance_id: str) -> Dict:
    """通过 DescribeDBInstanceAttribute 获取磁盘(GB)/CPU/内存(MB)。

    DescribeDBInstances 列表接口常不返回 CPU，故此处一并取 CPU/内存作为兜底来源。
    """
    attrs = {'storage_gb': '', 'cpu': '', 'memory_mb': None}
    try:
        req = rds_models.DescribeDBInstanceAttributeRequest(dbinstance_id=dbinstance_id)
        resp = client.describe_dbinstance_attribute(req)
        items = resp.body.items
        if items and items.dbinstance_attribute:
            attr = items.dbinstance_attribute[0]
            attrs['storage_gb'] = attr.dbinstance_storage or ''
            attrs['cpu'] = attr.dbinstance_cpu or ''
            attrs['memory_mb'] = attr.dbinstance_memory
    except Exception as e:
        logger.debug(f"  获取RDS属性失败 {dbinstance_id}: {e}")
    return attrs


def _parse_cpu_from_class(dbinstance_class: str) -> str:
    """从 DBInstanceClass 末尾的 'Nc' 后缀解析 vCPU 数，如 mysql.x2.large.2c -> 2"""
    if not dbinstance_class:
        return ''
    m = re.search(r'\.(\d+)c$', dbinstance_class.strip())
    return m.group(1) if m else ''


def _get_rds_tags(client: RdsClient, dbinstance_id: str, region_id: str) -> str:
    """通过 DescribeTags 获取 RDS 实例标签，格式 'key:value | key:value'。"""
    try:
        req = rds_models.DescribeTagsRequest(dbinstance_id=dbinstance_id, region_id=region_id)
        resp = client.describe_tags(req)
        tags = []
        items = resp.body.items
        if items and items.tag_infos:
            for ti in items.tag_infos:
                k = (ti.tag_key or '').strip()
                v = (ti.tag_value or '').strip()
                if k:
                    tags.append(f"{k}:{v}")
        return " | ".join(tags)
    except Exception as e:
        logger.debug(f"  获取RDS标签失败 {dbinstance_id}: {e}")
    return ''


def get_account_rds_instances(creds, role_session_name: str) -> List[Dict]:
    """遍历常用地域，获取所有 RDS 实例"""
    regions = ['cn-hangzhou', 'cn-beijing', 'cn-shanghai', 'cn-shenzhen',
               'cn-hongkong', 'ap-northeast-1', 'ap-southeast-1']
    all_instances = []

    for region in regions:
        client = create_rds_client(creds, region)
        page_number = 1

        while True:
            req = rds_models.DescribeDBInstancesRequest(
                region_id=region,
                page_size=100,
                page_number=page_number
            )
            try:
                resp = client.describe_dbinstances(req)
                items = resp.body.items
                if not items or not items.dbinstance:
                    break

                for inst in items.dbinstance:
                    dbinstance_id = inst.dbinstance_id
                    # 磁盘(GB)/CPU/内存: 属性接口一次取回；标签: DescribeTags
                    attrs = _get_rds_attrs(client, dbinstance_id)
                    # CPU 优先级: 列表 -> 属性 -> 解析规格类末尾 Nc 后缀(如 mysql.x2.large.2c -> 2)
                    cpu = inst.dbinstance_cpu or attrs['cpu'] or _parse_cpu_from_class(inst.dbinstance_class)
                    # 内存(MB): 列表 -> 属性
                    mem_mb = inst.dbinstance_memory or attrs['memory_mb']
                    mem_gb = int(round(mem_mb / 1024)) if mem_mb else ''
                    all_instances.append({
                        '资源所属账号': role_session_name,
                        'RegionId': region,
                        'DBInstanceId': dbinstance_id,
                        'DBInstanceDescription': inst.dbinstance_description or '',
                        'DBInstanceStatus': inst.dbinstance_status,
                        'Engine': inst.engine,
                        'EngineVersion': inst.engine_version,
                        'DBInstanceClass': inst.dbinstance_class,
                        'CPU(核)': cpu,
                        'memory(GB)': mem_gb,
                        'disk(GB)': attrs['storage_gb'],
                        '标签': _get_rds_tags(client, dbinstance_id, region),
                    })

                if len(items.dbinstance) < 100:
                    break
                page_number += 1

            except Exception as e:
                logger.debug(f"[{role_session_name}] 地域 {region} RDS查询失败(可忽略): {e}")
                break

    return all_instances


def write_csv(all_instances: List[Dict]):
    out_file = os.path.join(DATA_DIR, 'aliyun_rds_op.csv')
    fields = ['资源所属账号', 'RegionId', 'DBInstanceId', 'DBInstanceDescription',
              'DBInstanceStatus', 'Engine', 'EngineVersion', 'DBInstanceClass',
              'CPU(核)', 'memory(GB)', 'disk(GB)', '标签']
    with open(out_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for inst in all_instances:
            writer.writerow({k: inst.get(k, '') for k in fields})
    return out_file


def main():
    logger.info("=" * 40)
    logger.info("开始执行 RDS 资源盘点任务...")

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

            instances = get_account_rds_instances(creds, role_session_name)
            if instances:
                logger.info(f"  [{role_session_name}] 发现 {len(instances)} 个RDS实例")
                all_data.extend(instances)
            else:
                logger.info(f"  [{role_session_name}] 无RDS实例")

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    if all_data:
        out_path = write_csv(all_data)
        logger.info(f"RDS盘点完成: {len(all_data)} 个实例 -> {out_path}")
    else:
        logger.warning("RDS盘点完成: 未发现任何实例")


if __name__ == '__main__':
    main()

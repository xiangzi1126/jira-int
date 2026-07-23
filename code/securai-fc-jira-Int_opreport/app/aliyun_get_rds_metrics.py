"""获取 RDS 监控数据 (CPU使用率/内存使用率)"""
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_cms20190101 import models as cms_models
import configparser
import os
import csv
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict


def init_logger():
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(log_dir, 'rds_metrics.log'), encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))

NAMESPACE = 'acs_rds_dashboard'
METRICS = [
    ('CpuUsage', 'cpu'),
    ('MemoryUsage', 'memory'),
]


def get_last_month_range():
    today = datetime.today()
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    start = datetime(year, month, 1, 0, 0, 0)
    end = datetime(today.year, today.month, 1) - timedelta(seconds=1)
    billing_cycle = f'{year}-{month:02d}'
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S'), billing_cycle


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


def create_cms_client(creds) -> CmsClient:
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = 'metrics.cn-hangzhou.aliyuncs.com'
    return CmsClient(cfg)


def get_rds_instances(role_session_name: str) -> List[Dict]:
    """从配置或CSV获取RDS实例列表 (instanceId, instanceName)"""
    rds_file = os.path.join(DATA_DIR, 'aliyun_rds_op.csv')
    instances = []
    if not os.path.exists(rds_file):
        logger.warning(f"RDS盘点文件不存在: {rds_file}")
        return instances
    with open(rds_file, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('资源所属账号') == role_session_name:
                instances.append({
                    'instance_id': row.get('DBInstanceId', '').strip(),
                    'instance_name': row.get('DBInstanceDescription', row.get('DBInstanceId', '')).strip(),
                })
    return instances


def query_metrics(cms_client: CmsClient, instance_id: str, metric_name: str,
                  start_time: str, end_time: str) -> List[Dict]:
    """查询RDS监控指标"""
    all_datapoints = []
    cursor = None
    for _ in range(200):
        req = cms_models.DescribeMetricListRequest(
            namespace=NAMESPACE,
            metric_name=metric_name,
            period='7200',
            start_time=start_time,
            end_time=end_time,
            dimensions=json.dumps({'instanceId': instance_id}),
        )
        if cursor:
            req.cursor = cursor
        resp = cms_client.describe_metric_list(req)
        body = resp.body
        if body.code != '200':
            logger.error(f"RDS {instance_id} {metric_name} 查询失败: {body.message}")
            break
        datapoints = json.loads(body.datapoints or '[]')
        all_datapoints.extend(datapoints)
        cursor = body.next_token
        if not cursor:
            break
    return all_datapoints


def write_csv(account_dir: str, instance_name: str, metric_label: str,
              billing_cycle: str, role_session_name: str, datapoints: List[Dict]):
    """写入CSV"""
    import re
    fields = ['账期', '资源所属账号', '资源id', '资源名称', 'timestamp', 'average', 'maximum', 'minimum']
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', instance_name)
    out = os.path.join(account_dir, f'rds_{metric_label}_{safe_name}.csv')
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for dp in datapoints:
            writer.writerow({
                '账期': billing_cycle,
                '资源所属账号': role_session_name,
                '资源id': '',
                '资源名称': instance_name,
                'timestamp': dp.get('timestamp', ''),
                'average': dp.get('Average', ''),
                'maximum': dp.get('Maximum', ''),
                'minimum': dp.get('Minimum', ''),
            })
    return out


def main():
    logger.info("=" * 40)
    logger.info("开始执行 RDS 监控数据拉取任务...")

    config = read_config()
    role_sections = [s for s in config.sections() if s.startswith('aliyun-')]
    if not role_sections:
        logger.error("未找到任何 aliyun- 配置段")
        return

    start_time, end_time, billing_cycle = get_last_month_range()
    logger.info(f"账期: {billing_cycle}, 范围: {start_time} ~ {end_time}")

    valid_accounts = get_valid_op_accounts()
    total = 0

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

            instances = get_rds_instances(role_session_name)
            if not instances:
                logger.info(f"  [{role_session_name}] 无RDS实例")
                continue

            account_dir = os.path.join(DATA_DIR, f'监控数据_{role_session_name}')
            os.makedirs(account_dir, exist_ok=True)
            cms_client = create_cms_client(creds)

            for inst in instances:
                for metric_name, metric_label in METRICS:
                    logger.info(f"  拉取: {inst['instance_name']} / {metric_name}")
                    datapoints = query_metrics(
                        cms_client, inst['instance_id'], metric_name, start_time, end_time
                    )
                    if not datapoints:
                        logger.warning(f"    {inst['instance_name']} {metric_name} 无数据")
                        continue
                    out = write_csv(account_dir, inst['instance_name'], metric_label,
                                    billing_cycle, role_session_name, datapoints)
                    logger.info(f"    写入 {len(datapoints)} 条 -> {out}")
                    total += len(datapoints)

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    logger.info(f"RDS监控拉取完成，共 {total} 条数据")


if __name__ == '__main__':
    main()

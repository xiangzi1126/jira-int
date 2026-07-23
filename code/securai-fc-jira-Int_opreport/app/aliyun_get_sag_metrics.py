"""获取 SAG (智能接入网关) 监控数据 - 送信/受信帯域幅 (前3个月)

指标(acs_smartag):
  net_tx.rate  流出带宽 -> 送信帯域幅 (transmit)
  net_rx.rate  流入带宽 -> 受信帯域幅 (receive)
单位 bit/s,仅 Average 统计,原生周期 60/300s。

实现:以 300s(5分钟)粒度拉取全月数据,再聚合到 2 小时桶(与原 7200s 粒度一致,3个月约1080点,适合画图),
桶内 average/maximum/minimum 由 5分钟 Average 序列派生(最大値 = 桶内 5分钟均值的峰值),bit/s -> kbps。
输出每实例时序 CSV: 监控数据_<账号>/sag_{tx|rx}_<资源名称>.csv
"""
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
import re
from datetime import datetime, timedelta
from typing import List, Dict


NAMESPACE = 'acs_smartag'
# (metric_name, prefix, label)
METRICS = [
    ('net_tx.rate', 'tx', '送信'),   # 流出带宽 -> 送信
    ('net_rx.rate', 'rx', '受信'),   # 流入带宽 -> 受信
]
BUCKET_SECONDS = 7200  # 2 小时桶,与原 7200s 粒度一致


def init_logger():
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(log_dir, 'sag_metrics.log'), encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))


def get_last_n_months_range(n: int = 3):
    """返回前 n 个月的范围:从 n 个月前的1号 00:00:00 到 上月最后一天 23:59:59。"""
    today = datetime.today()
    end = datetime(today.year, today.month, 1) - timedelta(seconds=1)  # 上月最后一秒
    month_idx = today.year * 12 + (today.month - 1) - n
    start_year = month_idx // 12
    start_month = month_idx % 12 + 1
    start = datetime(start_year, start_month, 1, 0, 0, 0)
    billing_cycle = f"{start.strftime('%Y-%m')}~{end.strftime('%Y-%m')}"
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


def get_sag_instances(role_session_name: str) -> List[Dict]:
    """从 aliyun_sag_op.csv 获取 SAG 实例列表"""
    sag_file = os.path.join(DATA_DIR, 'aliyun_sag_op.csv')
    instances = []
    if not os.path.exists(sag_file):
        logger.warning(f"SAG盘点文件不存在: {sag_file}")
        return instances
    with open(sag_file, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('资源所属账号') == role_session_name:
                instances.append({
                    'instance_id': row.get('SmartAGId', '').strip(),
                    'instance_name': row.get('Name', row.get('SmartAGId', '')).strip(),
                })
    return instances


def query_metric_all_pages(cms_client: CmsClient, instance_id: str, metric_name: str,
                           start_time: str, end_time: str) -> List[Dict]:
    """查询指定指标 300s 粒度数据,自动翻页。失败返回空列表而不抛出。"""
    all_datapoints = []
    cursor = None
    for _ in range(200):
        req = cms_models.DescribeMetricListRequest(
            namespace=NAMESPACE,
            metric_name=metric_name,
            period='300',
            length='1000',
            start_time=start_time,
            end_time=end_time,
            dimensions=json.dumps({'instanceId': instance_id}),
        )
        if cursor:
            req.cursor = cursor
        try:
            resp = cms_client.describe_metric_list(req)
        except Exception as e:
            logger.error(f"实例 {instance_id} 指标 {metric_name} 查询异常: {e}")
            break
        body = resp.body
        if str(body.code) != '200':
            logger.error(f"实例 {instance_id} 指标 {metric_name} 查询失败: {body.message}")
            break
        datapoints = json.loads(body.datapoints or '[]')
        all_datapoints.extend(datapoints)
        cursor = body.next_token
        if not cursor:
            break
    return all_datapoints


def downsample_to_buckets(datapoints: List[Dict], bucket_seconds: int = BUCKET_SECONDS) -> List[Dict]:
    """将 300s 粒度 Average(bit/s) 聚合到 2 小时桶,输出 kbps 的 average/maximum/minimum。

    最大値 = 桶内 5分钟均值的峰值;bit/s -> kbps(/1000)。
    """
    if not datapoints:
        return []
    buckets = {}
    for dp in datapoints:
        ts_ms = dp.get('timestamp')
        if ts_ms is None:
            continue
        bucket_start_s = (int(ts_ms) // 1000 // bucket_seconds) * bucket_seconds
        val = dp.get('Average', 0)
        try:
            val = float(val) / 1000.0  # bit/s -> kbps
        except (TypeError, ValueError):
            val = 0.0
        buckets.setdefault(bucket_start_s, []).append(val)

    result = []
    for bucket_start_s in sorted(buckets):
        vals = buckets[bucket_start_s]
        result.append({
            'timestamp': bucket_start_s * 1000,
            'average': sum(vals) / len(vals),
            'maximum': max(vals),
            'minimum': min(vals),
        })
    return result


def write_instance_csv(account_dir: str, instance_id: str, instance_name: str, billing_cycle: str,
                       role_session_name: str, prefix: str, rows: List[Dict]):
    """每个实例每个指标写一个CSV: sag_<prefix>_<资源名称>.csv,值为 kbps"""
    fields = ['账期', '资源所属账号', '资源id', '资源名称', 'timestamp', 'average', 'maximum', 'minimum']
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', instance_name)
    out = os.path.join(account_dir, f'sag_{prefix}_{safe_name}.csv')
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                '账期': billing_cycle,
                '资源所属账号': role_session_name,
                '资源id': instance_id,
                '资源名称': instance_name,
                'timestamp': r['timestamp'],
                'average': f"{r['average']:.2f}",
                'maximum': f"{r['maximum']:.2f}",
                'minimum': f"{r['minimum']:.2f}",
            })
    return out


def main():
    logger.info("=" * 40)
    logger.info("🚀 开始执行 SAG [送信/受信帯域幅] 监控数据拉取任务(前3个月)...")

    config = read_config()
    role_sections = [s for s in config.sections() if s.startswith('aliyun-')]
    if not role_sections:
        logger.error("未找到任何 aliyun- 配置段")
        return

    start_time, end_time, billing_cycle = get_last_n_months_range(3)
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

            instances = get_sag_instances(role_session_name)
            if not instances:
                logger.info(f"  [{role_session_name}] 无SAG实例")
                continue

            account_dir = os.path.join(DATA_DIR, f'监控数据_{role_session_name}')
            os.makedirs(account_dir, exist_ok=True)
            cms_client = create_cms_client(creds)

            for inst in instances:
                iid = inst['instance_id']
                iname = inst['instance_name']
                for metric_name, prefix, label in METRICS:
                    logger.info(f"  拉取[{label}]: {iname} ({iid}) / {metric_name}")
                    dps = query_metric_all_pages(cms_client, iid, metric_name, start_time, end_time)
                    if not dps:
                        logger.warning(f"    {iname} {label} 无数据")
                        continue
                    rows = downsample_to_buckets(dps)
                    if not rows:
                        logger.warning(f"    {iname} {label} 降采样后无数据")
                        continue
                    out = write_instance_csv(account_dir, iid, iname, billing_cycle,
                                             role_session_name, prefix, rows)
                    logger.info(f"    [{label}] 写入 {len(rows)} 条(2h桶) -> {out}")
                    total += len(rows)

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    logger.info(f"SAG帯域幅监控拉取完成，共 {total} 条数据")


if __name__ == '__main__':
    main()

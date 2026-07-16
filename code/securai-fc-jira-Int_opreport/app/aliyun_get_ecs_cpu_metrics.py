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


def init_logger():
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(os.path.join(log_dir, 'ecs_cpu_metrics.log'), encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))


def get_last_month_range():
    today = datetime.today()
    # 上月年份和月份
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    start = datetime(year, month, 1, 0, 0, 0)
    # 上月最后一天：本月1日减1秒
    end = datetime(today.year, today.month, 1) - timedelta(seconds=1)
    billing_cycle = f'{year}-{month:02d}'
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S'), billing_cycle


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
    """从 jira_get_op_account.csv 读取需要出月报的资源所属账号列表"""
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

    logger.info(f"从 CSV 加载了 {len(valid_accounts)} 个需运维的账号。")
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


def get_ecs_instance_info_from_op_csv(role_session_name: str) -> Dict[str, str]:
    """【已修改】从 aliyun_ecs_op.csv 中提取该角色的ECS实例ID和资源名称的映射字典"""
    op_file = os.path.join(DATA_DIR, 'aliyun_ecs_op.csv')
    if not os.path.exists(op_file):
        logger.warning(f"  [跳过] ECS盘点文件不存在: {op_file}，请先执行 aliyun_get_op_ecs.py")
        return {}

    instance_info = {}
    with open(op_file, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            # 不再需要判断 ProductCode，因为这个文件里全是 ECS
            if row.get('资源所属账号') == role_session_name:
                # 这里的列名对应 aliyun_get_op_ecs.py 生成的表头
                rid = row.get('InstanceId', '').strip()
                rname = row.get('InstanceName', '').strip()

                # 防御性处理：如果“资源名称”为空，退化使用“资源id”作为名称
                if not rname:
                    rname = rid

                if rid and rid not in instance_info:
                    instance_info[rid] = rname

    if not instance_info:
        logger.warning(f"  [跳过] 在 aliyun_ecs_op.csv 中，未发现属于 [{role_session_name}] 的 ECS 资源记录。")
    else:
        logger.info(f"  [{role_session_name}] 从盘点文件提取到 {len(instance_info)} 个ECS实例")

    return instance_info


def create_cms_client(creds) -> CmsClient:
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = 'metrics.cn-hangzhou.aliyuncs.com'
    return CmsClient(cfg)


def query_cpu_metrics(cms_client: CmsClient, instance_id: str, start_time: str, end_time: str) -> List[Dict]:
    """查询单个实例的CPU利用率，自动翻页"""
    all_datapoints = []
    cursor = None

    for _ in range(200):
        req = cms_models.DescribeMetricListRequest(
            namespace='acs_ecs_dashboard',
            metric_name='CPUUtilization',
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
            logger.error(f"实例 {instance_id} 查询失败: {body.message}")
            break

        datapoints = json.loads(body.datapoints or '[]')
        all_datapoints.extend(datapoints)

        cursor = body.next_token
        if not cursor:
            break

    return all_datapoints


def write_instance_csv(account_dir: str, instance_id: str, instance_name: str, billing_cycle: str,
                       role_session_name: str, datapoints: List[Dict]):
    """每个实例写一个CSV文件，文件名使用资源名称"""
    fields = ['账期', '资源所属账号', '资源id', '资源名称', 'timestamp', 'average', 'maximum', 'minimum']
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', instance_name)
    out = os.path.join(account_dir, f'cpu_{safe_name}.csv')

    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for dp in datapoints:
            writer.writerow({
                '账期': billing_cycle,
                '资源所属账号': role_session_name,
                '资源id': instance_id,
                '资源名称': instance_name,
                'timestamp': dp.get('timestamp', ''),
                'average': dp.get('Average', ''),
                'maximum': dp.get('Maximum', ''),
                'minimum': dp.get('Minimum', ''),
            })
    return out


def main():
    logger.info("=" * 40)
    logger.info("🚀 开始执行 ECS CPU 监控数据拉取任务...")

    config = read_config()
    role_sections = [s for s in config.sections() if s.startswith('aliyun-')]

    if not role_sections:
        logger.error("🛑 未在 aliyun_config.ini 中找到任何以 'aliyun-' 开头的角色段落！脚本终止。")
        return
    else:
        logger.info(f"✅ 找到 {len(role_sections)} 个子账号 (aliyun-) 配置。")

    start_time, end_time, billing_cycle = get_last_month_range()

    logger.info(f"📅 计算获取的账期为: {billing_cycle}")
    logger.info(f"⏰ 监控查询时间范围: {start_time} 至 {end_time}")

    valid_accounts = get_valid_op_accounts()

    total = 0
    for role_section in role_sections:
        logger.info(f"\n{'─' * 30}\n🔍 正在处理配置段: {role_section}\n{'─' * 30}")
        try:
            target_session_name = config.get(role_section, 'role_session_name', fallback='')
            if not target_session_name:
                logger.warning(f"  [跳过] 配置段 {role_section} 缺失 role_session_name 参数。")
                continue

            if valid_accounts and target_session_name not in valid_accounts:
                logger.info(f"  [跳过] 账号 '{target_session_name}' 不在白名单过滤列表 (jira_get_op_account.csv) 中。")
                continue

            creds, role_session_name = get_sts_credentials(config, role_section)
            logger.info(f"  🔑 STS 授权成功，当前扮演账号: {role_session_name}")

            # 【已修改】调用新的函数从盘点文件获取实例信息，不再传 billing_cycle 去读账单
            instance_info = get_ecs_instance_info_from_op_csv(role_session_name)
            if not instance_info:
                continue

            account_dir = os.path.join(DATA_DIR, f'监控数据_{role_session_name}')
            os.makedirs(account_dir, exist_ok=True)

            cms_client = create_cms_client(creds)
            for instance_id, instance_name in instance_info.items():
                logger.info(f"  📊 正在拉取实例: {instance_name} ({instance_id})")
                datapoints = query_cpu_metrics(cms_client, instance_id, start_time, end_time)

                if not datapoints:
                    logger.warning(f"    [警告] 实例 {instance_name} 没有查询到任何监控数据，可能已关机或释放。")
                    continue

                out = write_instance_csv(account_dir, instance_id, instance_name, billing_cycle, role_session_name,
                                         datapoints)
                logger.info(f"    ✅ 成功写入 {len(datapoints)} 条 -> {out}")
                total += len(datapoints)

        except Exception as e:
            logger.error(f"角色 {role_section} 处理失败: {e}", exc_info=True)

    print(f"\n✅ 批处理完成！总计成功写入 {total} 条指标数据。")


if __name__ == '__main__':
    main()
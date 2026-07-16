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
            logging.FileHandler(os.path.join(log_dir, 'aliyun_op_cmd.log'), encoding='utf-8'),
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


def read_ecs_instances_from_op_csv() -> Dict[str, List[Dict]]:
    """读取 aliyun_ecs_op.csv，按账号分组存储实例信息"""
    op_file = os.path.join(DATA_DIR, 'aliyun_ecs_op.csv')
    accounts_instances = {}

    if not os.path.exists(op_file):
        logger.error(f"找不到ECS盘点文件: {op_file}，请先执行 aliyun_get_op_ecs.py")
        return accounts_instances

    with open(op_file, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            account = row.get('资源所属账号', '').strip()
            if not account:
                continue
            if account not in accounts_instances:
                accounts_instances[account] = []
            accounts_instances[account].append({
                'InstanceId': row.get('InstanceId', '').strip(),
                'InstanceName': row.get('InstanceName', '').strip()
            })

    total_instances = sum(len(v) for v in accounts_instances.values())
    logger.info(f"从 CSV 成功加载 {len(accounts_instances)} 个账号的 {total_instances} 台 ECS 实例。")
    return accounts_instances


def create_cms_client(creds) -> CmsClient:
    cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token
    )
    cfg.endpoint = 'metrics.cn-hangzhou.aliyuncs.com'
    return CmsClient(cfg)


def query_metric_all_pages(cms_client: CmsClient, instance_id: str, metric_name: str, start_time: str, end_time: str) -> \
List[Dict]:
    """查询指定指标的 5 分钟 (300秒) 间隔数据，自动翻页直到取完全月数据"""
    all_datapoints = []
    cursor = None

    for _ in range(20):
        req = cms_models.DescribeMetricListRequest(
            namespace='acs_ecs_dashboard',
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
            if resp.body.code != '200':
                logger.debug(f"实例 {instance_id} 查询指标 {metric_name} 失败/无数据: {resp.body.message}")
                break

            datapoints = json.loads(resp.body.datapoints or '[]')
            all_datapoints.extend(datapoints)

            cursor = resp.body.next_token
            if not cursor:
                break
        except Exception as e:
            logger.debug(f"API 请求异常 ({metric_name}): {e}")
            break

    return all_datapoints


def calculate_percentage_stats(dps: List[Dict]) -> tuple:
    """计算 CPU/Memory 等百分比指标的全月极值和平均值"""
    if not dps:
        return 'N/A', 'N/A', 'N/A'

    mx = max(dp.get('Maximum', dp.get('Average', 0)) for dp in dps)
    mn = min(dp.get('Minimum', dp.get('Average', 0)) for dp in dps)
    avg = sum(dp.get('Average', 0) for dp in dps) / len(dps)

    return f"{mn:.2f}", f"{mx:.2f}", f"{avg:.2f}"


def aggregate_disk_stats(used_dps: List[Dict], free_dps: List[Dict], total_dps: List[Dict]) -> Dict:
    """按盘符 (device) 聚合整月的磁盘状态，转换为 GB 和百分比"""
    disk_map = {}

    for dp in used_dps:
        dev = dp.get('device', dp.get('diskname', 'unknown'))
        if dev not in disk_map:
            disk_map[dev] = {'used_max': 0, 'free_min': float('inf'), 'total': 0}
        val = dp.get('Maximum', dp.get('Average', 0))
        disk_map[dev]['used_max'] = max(disk_map[dev]['used_max'], val)

    for dp in free_dps:
        dev = dp.get('device', dp.get('diskname', 'unknown'))
        if dev not in disk_map:
            disk_map[dev] = {'used_max': 0, 'free_min': float('inf'), 'total': 0}
        val = dp.get('Minimum', dp.get('Average', 0))
        disk_map[dev]['free_min'] = min(disk_map[dev]['free_min'], val)

    for dp in total_dps:
        dev = dp.get('device', dp.get('diskname', 'unknown'))
        if dev not in disk_map:
            disk_map[dev] = {'used_max': 0, 'free_min': float('inf'), 'total': 0}
        val = dp.get('Maximum', dp.get('Average', 0))
        disk_map[dev]['total'] = max(disk_map[dev]['total'], val)

    formatted_disks = {}
    for dev, stats in disk_map.items():
        used_gb = stats['used_max'] / (1024 ** 3)
        free_gb = stats['free_min'] / (1024 ** 3) if stats['free_min'] != float('inf') else 0

        if stats['total'] > 0:
            total_gb = stats['total'] / (1024 ** 3)
        else:
            total_gb = used_gb + free_gb

        # 【修改点】计算百分比最大使用率
        if total_gb > 0:
            used_percent = (used_gb / total_gb) * 100
        else:
            used_percent = 0

        formatted_disks[dev] = {
            'SizeGB': f"{total_gb:.2f}",
            'FreeGB': f"{free_gb:.2f}",
            'UsedPercent': f"{used_percent:.2f}"
        }

    return formatted_disks


def main():
    logger.info("=" * 40)
    logger.info("🚀 开始执行 全局 ECS [CMD运维指标] (上月5分钟间隔) 拉取聚合任务...")

    config = read_config()
    start_time, end_time, billing_cycle = get_last_month_range()

    logger.info(f"📅 统计账期为: {billing_cycle}")
    logger.info(f"⏰ 查询时间范围: {start_time} 至 {end_time}")

    accounts_instances = read_ecs_instances_from_op_csv()
    if not accounts_instances:
        logger.error("🛑 无任何待处理实例，脚本终止。")
        return

    # 【修改点】更新输出 CSV 表头
    out_file = os.path.join(DATA_DIR, 'aliyun_op_cmd_summary.csv')
    fields = [
        '账期', '资源所属账号', 'InstanceId', 'InstanceName',
        'CPU最小使用率(%)', 'CPU最大使用率(%)', 'CPU平均使用率(%)',
        '内存最小使用率(%)', '内存最大使用率(%)', '内存平均使用率(%)',
        '盘符', '磁盘大小(GB)', '磁盘剩余大小(GB)', '磁盘最大使用率（%)'
    ]

    with open(out_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        role_sections = [s for s in config.sections() if s.startswith('aliyun-')]
        for role_section in role_sections:
            target_account = config.get(role_section, 'role_session_name', fallback='')
            if target_account not in accounts_instances:
                continue

            logger.info(
                f"\n{'─' * 30}\n🔍 正在处理账号: {target_account} (共 {len(accounts_instances[target_account])} 台)\n{'─' * 30}")
            try:
                creds, role_session_name = get_sts_credentials(config, role_section)
                cms_client = create_cms_client(creds)

                for inst in accounts_instances[target_account]:
                    iid = inst['InstanceId']
                    iname = inst['InstanceName']
                    logger.info(f"  📊 聚合数据中 -> {iname} ({iid})")

                    cpu_dps = query_metric_all_pages(cms_client, iid, 'CPUUtilization', start_time, end_time)
                    c_min, c_max, c_avg = calculate_percentage_stats(cpu_dps)

                    mem_dps = query_metric_all_pages(cms_client, iid, 'memory_usedutilization', start_time, end_time)
                    m_min, m_max, m_avg = calculate_percentage_stats(mem_dps)

                    disk_used_dps = query_metric_all_pages(cms_client, iid, 'diskusage_used', start_time, end_time)
                    disk_free_dps = query_metric_all_pages(cms_client, iid, 'diskusage_free', start_time, end_time)
                    disk_total_dps = query_metric_all_pages(cms_client, iid, 'diskusage_total', start_time, end_time)

                    disk_stats = aggregate_disk_stats(disk_used_dps, disk_free_dps, disk_total_dps)

                    if not disk_stats:
                        writer.writerow({
                            '账期': billing_cycle, '资源所属账号': target_account,
                            'InstanceId': iid, 'InstanceName': iname,
                            'CPU最小使用率(%)': c_min, 'CPU最大使用率(%)': c_max, 'CPU平均使用率(%)': c_avg,
                            '内存最小使用率(%)': m_min, '内存最大使用率(%)': m_max, '内存平均使用率(%)': m_avg,
                            '盘符': 'N/A', '磁盘大小(GB)': 'N/A', '磁盘剩余大小(GB)': 'N/A', '磁盘最大使用率（%)': 'N/A'
                        })
                    else:
                        for dev, stat in disk_stats.items():
                            writer.writerow({
                                '账期': billing_cycle, '资源所属账号': target_account,
                                'InstanceId': iid, 'InstanceName': iname,
                                'CPU最小使用率(%)': c_min, 'CPU最大使用率(%)': c_max, 'CPU平均使用率(%)': c_avg,
                                '内存最小使用率(%)': m_min, '内存最大使用率(%)': m_max, '内存平均使用率(%)': m_avg,
                                '盘符': dev,
                                '磁盘大小(GB)': stat['SizeGB'],
                                '磁盘剩余大小(GB)': stat['FreeGB'],
                                '磁盘最大使用率（%)': stat['UsedPercent']
                            })

            except Exception as e:
                logger.error(f"账号 {target_account} 处理失败: {e}", exc_info=True)

    logger.info(f"\n✅ 聚合任务处理完成！汇总数据已保存至: {out_file}")


if __name__ == '__main__':
    main()
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
import configparser
import os
import sys
from typing import List, Dict, Any
from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models
from alibabacloud_tea_util import models as util_models
import logging
import csv
from datetime import datetime, timedelta


# ===================== 日志初始化配置 =====================
def init_logger():
    """初始化日志，输出到 ../../jira/log/create_issue.log"""
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'create_issue.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()

# ===================== 路径配置 =====================
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))
OUTPUT_FILE_PATH = ''


class AliyunMultiRoleBillQuery:
    @staticmethod
    def get_all_role_sections() -> List[str]:
        """自动识别配置文件中所有aliyun-开头的角色分组"""
        config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
        config = configparser.ConfigParser()

        if not os.path.exists(config_path):
            logger.error(f"配置文件不存在：{config_path}")
            raise FileNotFoundError(f"配置文件不存在：{config_path}")
        config.read(config_path, encoding='utf-8')
        logger.info(f"已加载配置文件：{config_path}")

        role_sections = [section for section in config.sections() if section.startswith('aliyun-')]
        if not role_sections:
            logger.error("配置文件中未找到任何aliyun-开头的角色分组")
            raise ValueError("配置文件中未找到任何aliyun-开头的角色分组")
        return role_sections

    @staticmethod
    def get_master_credentials() -> tuple[str, str]:
        """读取主账号AK"""
        config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        try:
            ak_id = config.get('aliyun', 'access_key_id')
            ak_secret = config.get('aliyun', 'access_key_secret')
            return ak_id, ak_secret
        except Exception as e:
            logger.error(f"主账号配置读取失败: {e}")
            raise

    @staticmethod
    def create_client_by_role(role_section: str) -> BssOpenApi20171214Client:
        """创建BSS客户端"""
        master_ak_id, master_ak_secret = AliyunMultiRoleBillQuery.get_master_credentials()
        config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        role_arn = config.get(role_section, 'role_arn')
        role_session_name = config.get(role_section, 'role_session_name')

        sts_config = open_api_models.Config(access_key_id=master_ak_id, access_key_secret=master_ak_secret)
        sts_config.endpoint = 'sts.aliyuncs.com'
        sts_client = StsClient(sts_config)

        response = sts_client.assume_role(AssumeRoleRequest(
            role_arn=role_arn, role_session_name=role_session_name, duration_seconds=3600
        ))

        creds = response.body.credentials
        bss_config = open_api_models.Config(
            access_key_id=creds.access_key_id,
            access_key_secret=creds.access_key_secret,
            security_token=creds.security_token
        )
        bss_config.endpoint = "business.ap-southeast-1.aliyuncs.com"
        return BssOpenApi20171214Client(bss_config)

    @staticmethod
    def get_last_month_billing_cycle() -> str:
        today = datetime.today()
        last_month = today.replace(day=1) - timedelta(days=1)
        return last_month.strftime("%Y-%m")

    @staticmethod
    def process_single_role_bill(response_body: Dict[str, Any], role_section: str) -> List[Dict[str, Any]]:
        """处理账单数据，包含详细处理日志，不再过滤0元账单"""
        logger.info(f"[{role_section}] >>> 开始详尽数据处理流...")

        if not response_body.get('Success') or not response_body.get('Data'):
            logger.warning(f"[{role_section}] 无有效数据响应")
            return []

        data = response_body['Data']
        bill_items = data.get('Items', [])
        bill_items = bill_items if isinstance(bill_items, list) else [bill_items] if bill_items else []

        total_raw = len(bill_items)
        no_id_count = 0
        instance_group = {}

        for i, item in enumerate(bill_items):
            instance_id = item.get('InstanceID', '')
            product_code = item.get('ProductCode', 'Unknown')
            pretax_amount = round(float(item.get('PretaxAmount', 0)), 5)

            # 原始数据日志
            logger.info(
                f"  [原始记录 {i + 1}/{total_raw}] 实例ID: {instance_id or 'N/A'} | 产品代码: {product_code} | 金额: {pretax_amount}")

            if not instance_id:
                no_id_count += 1
                logger.info(f"    └─ [跳过] 原因: 缺失 InstanceID")
                continue

            # 标签解析日志
            tags_str = item.get('Tag', '').strip()
            tag_list = [t.strip() for t in tags_str.split('|') if t.strip()] if tags_str else []
            if tag_list:
                logger.debug(f"    └─ [标签] 解析到: {tag_list}")

            nickname = item.get('NickName', '').strip()

            if instance_id not in instance_group:
                logger.info(f"    └─ [新增记录] 实例 {instance_id} 进入结果集")
                instance_group[instance_id] = {
                    '账期': data.get('BillingCycle', ''),
                    '资源所属账号': item.get('BillAccountName', ''),
                    'ProductCode': product_code,
                    'ProductType': item.get('ProductType', ''),
                    '资源类型': item.get('ProductDetail', ''),
                    '资源id': instance_id,
                    '资源名称': nickname,
                    '资源所在地域': item.get('Region', ''),
                    '资源付费方式': set(),
                    '消费金额': 0.0,
                    '币种': item.get('Currency', 'CNY'),
                    '标签列表': set(tag_list),
                    '描述': str(item.get('InstanceConfig', ''))[:100]
                }
            else:
                old_amt = instance_group[instance_id]['消费金额']
                new_amt = round(old_amt + pretax_amount, 5)
                logger.info(f"    └─ [合并金额] 实例 {instance_id}: {old_amt} -> {new_amt}")

                if not instance_group[instance_id]['资源名称'] and nickname:
                    instance_group[instance_id]['资源名称'] = nickname
                if tag_list:
                    instance_group[instance_id]['标签列表'].update(tag_list)

            instance_group[instance_id]['消费金额'] = round(instance_group[instance_id]['消费金额'] + pretax_amount, 5)
            sub_type = item.get('SubscriptionType', '')
            pay_type = '包年包月' if sub_type == 'Subscription' else '按量付费' if sub_type == 'PayAsYouGo' else sub_type
            instance_group[instance_id]['资源付费方式'].add(pay_type)

        # 转换为最终列表
        processed_details = []
        for inst_data in instance_group.values():
            inst_data['标签'] = ' | '.join(inst_data.pop('标签列表'))
            inst_data['资源付费方式'] = '/'.join(filter(None, inst_data['资源付费方式'])) or '未知'
            processed_details.append(inst_data)

        logger.info(
            f"[{role_section}] 处理统计: 原始={total_raw}, 无ID跳过={no_id_count}, 最终合并实例={len(processed_details)}")
        return processed_details

    @staticmethod
    def init_output_file():
        os.makedirs(DATA_DIR, exist_ok=True)
        fields = [
            '账期', '资源所属账号', 'ProductCode', 'ProductType', '资源类型',
            '资源id', '资源名称', '资源所在地域', '资源付费方式', '消费金额',
            '币种', '标签', '描述'
        ]
        with open(OUTPUT_FILE_PATH, 'w', encoding='utf-8-sig', newline='') as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()
        logger.info(f"输出文件初始化: {OUTPUT_FILE_PATH}")

    @staticmethod
    def append_to_output_file(detail_list: List[Dict[str, Any]]):
        if not detail_list: return
        fields = [
            '账期', '资源所属账号', 'ProductCode', 'ProductType', '资源类型',
            '资源id', '资源名称', '资源所在地域', '资源付费方式', '消费金额',
            '币种', '标签', '描述'
        ]
        with open(OUTPUT_FILE_PATH, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writerows(detail_list)

    @staticmethod
    def query_single_role(role_section: str, billing_cycle: str) -> List[Dict[str, Any]]:
        try:
            client = AliyunMultiRoleBillQuery.create_client_by_role(role_section)
            next_token = None
            all_items = []

            for attempt in range(100):
                req = bss_open_api_20171214_models.DescribeInstanceBillRequest(
                    billing_cycle=billing_cycle, max_results=300, next_token=next_token,is_billing_item=True
                )
                resp = client.describe_instance_bill_with_options(req, util_models.RuntimeOptions())
                body = resp.body.to_map() if hasattr(resp.body, 'to_map') else dict(resp.body)

                items = body.get('Data', {}).get('Items', [])
                all_items.extend(items if isinstance(items, list) else [items] if items else [])

                next_token = body.get('Data', {}).get('NextToken') or body.get('NextToken')
                if not next_token: break

            return AliyunMultiRoleBillQuery.process_single_role_bill(
                {'Success': True, 'Data': {'BillingCycle': billing_cycle, 'Items': all_items}},
                role_section
            )
        except Exception as e:
            logger.error(f"角色 {role_section} 查询失败: {e}", exc_info=True)
            return []

    @staticmethod
    def main(args: List[str]) -> None:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('-r', '--role')
        parser.add_argument('-c', '--cycle')
        parsed = parser.parse_args(args)

        role_sections = [parsed.role] if parsed.role else AliyunMultiRoleBillQuery.get_all_role_sections()
        cycle = parsed.cycle or AliyunMultiRoleBillQuery.get_last_month_billing_cycle()

        global OUTPUT_FILE_PATH
        OUTPUT_FILE_PATH = os.path.join(DATA_DIR, f'aliyun_bill_{cycle}.csv')
        AliyunMultiRoleBillQuery.init_output_file()

        total = 0
        for role in role_sections:
            logger.info(f"\n{'-' * 30}\n处理角色: {role}\n{'-' * 30}")
            details = AliyunMultiRoleBillQuery.query_single_role(role, cycle)
            AliyunMultiRoleBillQuery.append_to_output_file(details)
            total += len(details)

        print(f"\n✅ 处理完成！总计写入 {total} 条明细 (包含 0 元账单)。")
        print(f"📄 文件路径: {OUTPUT_FILE_PATH}")


if __name__ == '__main__':
    AliyunMultiRoleBillQuery.main(sys.argv[1:])
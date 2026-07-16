# -*- coding: utf-8 -*-
"""
阿里云包月弹性公网 IP (EIP) 开放 API 真实续费开销询价工具（国际/国内通用版）
功能：
1. 调用 VPC 原生接口 DescribeEipAddresses 实时拉取 EIP 带宽；
2. 严格按规范组装 ModuleList 获取 API 续费价格 (国际站自动返回 USD)；
3. 【核心修正】：严格遵循 BSS OpenAPI 针对 EIP 的小写 bandwidth 传参规范。
4. 【核心修正】：BSS 财务中心 Endpoint 已替换为国际站专属节点 (ap-southeast-1)。
5. 【严格对齐】：严格对齐原有 CSV 的 9 个基础列名，防止追加写入时数据错乱。
6. 【极具可视化的探针全显】：强制以高可读性格式在控制台全量输出属性字典与询价 Payload。
7. 【时间筛选增强】：自动计算并严格过滤出“下月到期”的资源进行精准询价。
"""
import os
import sys
import csv
import re
import json
import configparser
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

# ===================== 阿里云 SDK 依赖导入 =====================
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models

from alibabacloud_vpc20160428.client import Client as Vpc20160428Client
from alibabacloud_vpc20160428 import models as vpc_20160428_models

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/eip_renewal_price.log'))


# ===================== 可视化日志系统配置 =====================
class VisualLogger:
    """提供极具可视化排版的结构化日志系统"""

    def __init__(self):
        log_dir = os.path.dirname(LOG_FILE)
        os.makedirs(log_dir, exist_ok=True)
        log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
        file_handler.setFormatter(log_format)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_format)

        self.logger = logging.getLogger('eip_renewal_visual')
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)

    def info(self, msg: str):
        self.logger.info(msg)

    def warning(self, msg: str):
        self.logger.warning(msg)

    def error(self, msg: str):
        self.logger.error(msg)

    def print_block(self, title: str, data: Any, level: int = logging.INFO):
        """深度美化打印 JSON/字典 等结构化数据，用于极致的可视化追踪"""
        border = "═" * 80
        if isinstance(data, (dict, list)):
            try:
                formatted_data = json.dumps(data, indent=4, ensure_ascii=False)
            except Exception:
                formatted_data = str(data)
        else:
            formatted_data = str(data)

        log_msg = f"\n{border}\n❖ {title} ❖\n{border}\n{formatted_data}\n{border}\n"
        self.logger.log(level, log_msg)

    def print_step(self, step_name: str):
        """打印主干流程节点"""
        self.logger.info(f"\n" + "★" * 30 + f" [STEP: {step_name}] " + "★" * 30)


logger = VisualLogger()


class AliyunEipRenewalPriceQuery:
    @staticmethod
    def sanitize_session_name(name: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', name)
        return sanitized[:64] if len(sanitized) >= 2 else "STS_Session"

    @staticmethod
    def get_master_credentials() -> tuple[str, str]:
        logger.info(f"⚙️ 正在从主配置路径读取AK凭证: {CONFIG_PATH}")
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        try:
            return config.get('aliyun', 'access_key_id'), config.get('aliyun', 'access_key_secret')
        except Exception as e:
            logger.error(f"❌ 错误: 主账号配置读取失败: {str(e)}")
            raise

    @staticmethod
    def get_sts_credentials_by_role(role_section: str) -> Any:
        logger.info(f"🔄 正在为配置节点 [{role_section}] 换取 STS 临时令牌...")
        master_ak_id, master_ak_secret = AliyunEipRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunEipRenewalPriceQuery.sanitize_session_name(raw_session_name)
        sts_config = open_api_models.Config(access_key_id=master_ak_id, access_key_secret=master_ak_secret,
                                            endpoint='sts.aliyuncs.com')
        sts_client = StsClient(sts_config)

        assume_role_request = AssumeRoleRequest(role_arn=role_arn, role_session_name=api_safe_session_name,
                                                duration_seconds=3600)
        return sts_client.assume_role(assume_role_request).body.credentials

    @staticmethod
    def get_session_name_to_section_map() -> Dict[str, str]:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        mapping = {}
        for section in config.sections():
            if section.startswith('aliyun-') and config.has_option(section, 'role_session_name'):
                session_name = config.get(section, 'role_session_name').strip()
                if session_name:
                    mapping[session_name] = section
        logger.print_block("Loaded Account Role Map", mapping)
        return mapping

    @staticmethod
    def load_duration_mapping() -> Dict[str, float]:
        sbu_csv_path = os.path.join(DATA_DIR, 'jira_get_renewal_sbu_issues.csv')
        duration_map = {}
        if not os.path.exists(sbu_csv_path):
            logger.warning(f"⚠️ 警告: 未找到 Jira 周期工单文件 {sbu_csv_path}，所有资源将默认按 1.0 个月进行询价。")
            return duration_map

        try:
            with open(sbu_csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    instance_id = row.get('ID', '').strip()
                    duration_str = row.get('续费时长（月）', '1.0').strip()
                    if instance_id:
                        try:
                            duration_map[instance_id] = float(duration_str)
                        except ValueError:
                            duration_map[instance_id] = 1.0
        except Exception as e:
            logger.error(f"❌ 错误: 读取 Jira 子任务工单失败: {str(e)}")

        logger.info(f"成功加载 {len(duration_map)} 条 Jira 续费参数配置")
        return duration_map

    @staticmethod
    def fetch_eip_physical_specs(vpc_client: Vpc20160428Client, region_id: str, instance_id: str) -> int:
        """调用 VPC 官方原生接口精准探测 EIP 物理带宽指纹"""
        logger.info(f"🔍 [VPC Probe] 正在向地域 [{region_id}] 检索 EIP 规格明细: {instance_id}")
        try:
            request = vpc_20160428_models.DescribeEipAddressesRequest(
                region_id=region_id,
                allocation_id=instance_id
            )
            response = vpc_client.describe_eip_addresses_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            eip_addresses_list = res_body.get('EipAddresses', {}).get('EipAddress', [])
            if not eip_addresses_list:
                raise ValueError("VPC 未能返回此 EIP 实例记录，资源可能已释放。")

            eip_info = eip_addresses_list[0]
            logger.print_block(f"VPC DescribeEipAddresses Result - [{instance_id}]", eip_info)

            bandwidth = int(eip_info.get('Bandwidth', 5))
            logger.info(f"👉 [VPC Probe] 探测成功 -> 提取带宽: {bandwidth} Mbps")
            return bandwidth
        except Exception as e:
            logger.error(f"❌ [VPC Probe] 探测崩溃: {str(e)}")
            raise

    @staticmethod
    def query_eip_price_via_bss(
            bss_client: BssOpenApi20171214Client,
            vpc_client: Vpc20160428Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, int]:
        """获取费用中心询价结果，并返回带宽值 (国际站会自动返回 USD)"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', '').strip()

        try:
            bandwidth = AliyunEipRenewalPriceQuery.fetch_eip_physical_specs(vpc_client, region_id, instance_id)

            # ========================== 核心修复区 ==========================
            # 严格使用您提供的“正确写法”，保障 BSS 对于 EIP 的校验机制不报错
            module_list_0 = bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='bandwidth',  # 强制小写 bandwidth
                config=f'bandwidth:{bandwidth}'  # 强制小写 bandwidth
            )

            request = bss_open_api_20171214_models.GetSubscriptionPriceRequest(
                service_period_unit=period_unit,
                subscription_type='Subscription',
                product_code='eip',
                order_type='Renewal',
                instance_id=instance_id,
                region=region_id,
                service_period_quantity=period_quantity,
                module_list=[
                    module_list_0
                ]
            )
            # ================================================================

            # 仅做打印日志用途，方便排错
            request_log_payload = {
                "service_period_unit": period_unit,
                "subscription_type": 'Subscription',
                "product_code": 'eip',
                "order_type": 'Renewal',
                "instance_id": instance_id,
                "region": region_id,
                "service_period_quantity": period_quantity,
                "module_list": [{"module_code": 'bandwidth', "config": f'bandwidth:{bandwidth}'}]
            }
            logger.print_block(f"BSS GetSubscriptionPrice Payload - [{instance_id}]", request_log_payload)

            response = bss_client.get_subscription_price_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            logger.print_block(f"BSS Raw Response - [{instance_id}]", res_body)

            is_success = res_body.get('Success')

            if is_success:
                data = res_body.get('Data', {})
                trade_price = float(data.get('TradePrice', 0.0))
                original_price = float(data.get('OriginalPrice', 0.0))
                currency = data.get('Currency', 'CNY')  # 核心：国际站将自动返回 USD
                return trade_price, original_price, currency, bandwidth
            else:
                msg = res_body.get('Message', 'API 询价失败')
                code = res_body.get('Code', '')
                logger.error(f"❌ [API 返回异常] 状态码: {code} | 详情: {msg}")
                return -1.0, -1.0, f"BSS拒绝: [{code}] {msg}", 0

        except Exception as e:
            logger.error(f"❌ [询价异常] {str(e)}")
            return -1.0, -1.0, f"技术异常: {str(e)}", 0

    @staticmethod
    def main():
        logger.print_step("PROGRAM START: ELASTIC IP (EIP) RENEWAL PRICE QUERY (GLOBAL EDITION)")

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        first_day_of_next_month = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month_str = first_day_of_next_month.strftime("%Y-%m")
        current_month_str = today.strftime("%Y-%m")
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        time_info = {
            "Last Month (Source Bill)": last_month_str,
            "Current Month": current_month_str,
            "Target Expiry Month (Filter)": next_month_str
        }
        logger.print_block("Time Period Calculation", time_info)

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置到期数据清单 {input_csv_path}。")
            sys.exit(1)

        session_to_section = AliyunEipRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunEipRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                instance_id = row.get('资源id', '').strip()
                product_code = row.get('产品代码', '').lower().strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()

                # 标准化时间格式
                expire_time_norm = expire_time_raw.replace('/', '-')

                if product_code == 'eip' or instance_id.startswith('eip-'):
                    if account:
                        if expire_time_norm.startswith(next_month_str):
                            if account not in instances_by_account:
                                instances_by_account[account] = []
                            instances_by_account[account].append(row)
                            filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【EIP弹性公网IP + 下月到期】条件的实例。")

        # ⭐ 【严格对齐的表头】：与原有 aliyun_renewal_price CSV 一模一样
        output_fields = [
            '资源id', '资源所属账号', '资源到期时间', '产品代码',
            '描述', '原价', '折扣', '货币单位', '最终价'
        ]

        file_exists = os.path.exists(output_csv_path)
        with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            if not file_exists:
                writer.writeheader()

        total_processed, total_success, total_failed = 0, 0, 0

        logger.print_step("STARTING API QUERY PROCESS")
        for account, items in instances_by_account.items():
            logger.info(f"\n▶▶ 🏁 账号集群: [{account}] | 待处理EIP总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunEipRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token
                )

                # VPC 属于业务接口，全球通用，维持原样
                open_config.endpoint = 'vpc.aliyuncs.com'
                vpc_client = Vpc20160428Client(open_config)

                # BSS 属于财务接口，对国际站(alibabacloud)必须指定 ap-southeast-1 节点
                open_config.endpoint = 'business.ap-southeast-1.aliyuncs.com'
                bss_client = BssOpenApi20171214Client(open_config)

            except Exception as e:
                logger.error(f"❌ 初始化失败: {str(e)}")
                continue

            processed_rows = []
            for item in items:
                total_processed += 1
                instance_id = item.get('资源id', '').strip()

                duration_val = duration_map.get(instance_id, 1.0)
                period_unit, period_quantity = ('Year', 1) if duration_val == 12.0 else ('Month', (
                    int(duration_val) if duration_val > 0 else 1))

                trade_price, original_price, status_or_currency, bandwidth = AliyunEipRenewalPriceQuery.query_eip_price_via_bss(
                    bss_client, vpc_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': item.get('产品代码', 'eip'),
                    '描述': '',
                    '原价': '',
                    '折扣': '',
                    '货币单位': '',
                    '最终价': ''
                }

                if trade_price >= 0:
                    total_success += 1
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency

                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0

                    out_row['描述'] = f"带宽: {bandwidth} Mbps"
                    logger.info(f"✅ 询价成功! ID: {instance_id} | 最终价: {trade_price} {status_or_currency}")
                else:
                    total_failed += 1
                    out_row['描述'] = f"API受阻: {status_or_currency}"
                    logger.error(f"❌ API拒绝: ID: {instance_id} | {status_or_currency}")

                processed_rows.append(out_row)

            # 追加写入
            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=output_fields)
                writer.writerows(processed_rows)

        logger.print_step("EXECUTION COMPLETE")
        logger.print_block("Final Details", {
            "Total Processed": total_processed,
            "Success": total_success,
            "Failed": total_failed,
            "Output Path": output_csv_path,
            "Write Mode": "Append ('a')"
        })


if __name__ == '__main__':
    AliyunEipRenewalPriceQuery.main()
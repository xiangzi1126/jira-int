# -*- coding: utf-8 -*-
"""
阿里云包月 高速通道-物理专线 (pconn) 开放 API 真实续费开销询价工具 (国际/国内通用版)
功能：
1. 调用 VPC 接口 DescribePhysicalConnections 实时拉取专线物理规格 (Spec)；
2. 组合复合商品 ModuleList 提交给 BSS 费用中心 (国际站自动返回 USD)；
3. 【终极修正版】：严格按照官方 ConfigList: ["Region", "Spec"] 组装，移除多余的 PortType。
4. 【核心修正】：BSS 财务中心 Endpoint 已替换为国际站专属节点 (ap-southeast-1)。
5. 【极具可视化的探针全显】：强制以高可读性格式在控制台全量输出属性字典与询价 Payload。
6. 【时间筛选增强】：自动计算并严格过滤出“下月到期”的资源进行精准询价。
"""
import os
import sys
import csv
import re
import configparser
import logging
import json
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
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/pconn_renewal_price.log'))


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

        self.logger = logging.getLogger('pconn_renewal_visual')
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


# ===================== 核心业务类 =====================
class AliyunPconnRenewalPriceQuery:
    @staticmethod
    def sanitize_session_name(name: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', name)
        return sanitized[:64] if len(sanitized) >= 2 else "STS_Session"

    @staticmethod
    def get_master_credentials() -> Tuple[str, str]:
        logger.info(f"⚙️ 正在从主配置路径读取AK凭证: {CONFIG_PATH}")
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        try:
            return config.get('aliyun', 'access_key_id'), config.get('aliyun', 'access_key_secret')
        except Exception as e:
            logger.error(f"❌ 主账号配置读取失败: {str(e)}")
            raise

    @staticmethod
    def get_sts_credentials_by_role(role_section: str) -> Any:
        logger.info(f"🔄 正在为配置节点 [{role_section}] 换取 STS 临时令牌...")
        master_ak_id, master_ak_secret = AliyunPconnRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunPconnRenewalPriceQuery.sanitize_session_name(raw_session_name)
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
            logger.error(f"❌ 读取工单失败: {str(e)}")

        logger.info(f"成功加载 {len(duration_map)} 条 Jira 续费参数配置")
        return duration_map

    @staticmethod
    def fetch_pconn_physical_specs(vpc_client: Vpc20160428Client, region_id: str, instance_id: str) -> Dict[str, Any]:
        """调用 VPC 接口精准提取物理专线 (pconn) 的所有计费强关联属性"""
        logger.info(f"🔍 [VPC Probe] 正在向地域 [{region_id}] 检索专线规格明细: {instance_id}")
        try:
            request = vpc_20160428_models.DescribePhysicalConnectionsRequest(
                region_id=region_id,
                filter=[
                    vpc_20160428_models.DescribePhysicalConnectionsRequestFilter(
                        key="PhysicalConnectionId",
                        value=[instance_id]
                    )
                ]
            )
            response = vpc_client.describe_physical_connections_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            pconn_list = res_body.get('PhysicalConnectionSet', {}).get('PhysicalConnectionType', [])
            if not pconn_list:
                raise ValueError("VPC 未能返回此物理专线实例记录，资源可能已释放或没有权限访问。")

            pconn_info = pconn_list[0]
            logger.print_block(f"VPC DescribePhysicalConnections Result - [{instance_id}]", pconn_info)

            # 提取物理专线计费的核心组件 Spec
            specs = {
                'Spec': str(pconn_info.get('Spec', '10G'))
            }
            logger.info(f"👉 [VPC Probe] 探测成功 -> 提取出的专线计费规格指纹: {specs}")
            return specs
        except Exception as e:
            logger.error(f"❌ [VPC Probe] 探测崩溃: {str(e)}")
            raise

    @staticmethod
    def query_pconn_price_via_bss(
            bss_client: BssOpenApi20171214Client,
            vpc_client: Vpc20160428Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, Dict[str, Any]]:
        """组合多组件进行询价 (国际站将自动返回 USD)"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', '').strip()

        try:
            # 1. 获取物理专线的核心计费指纹
            specs = AliyunPconnRenewalPriceQuery.fetch_pconn_physical_specs(vpc_client, region_id, instance_id)

            # 2. 严格按照官网要求的 ConfigList: ["Region", "Spec"] 进行组装
            module_region = bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='Region', config=f"Region:{region_id}"
            )
            module_spec = bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='Spec', config=f"Spec:{specs['Spec']}"
            )

            # 3. 日志记录组装参数
            request_log_payload = {
                "subscription_type": 'Subscription',
                "service_period_unit": period_unit,
                "product_code": 'pconn',
                "order_type": 'Renewal',
                "instance_id": instance_id,
                "region": region_id,
                "service_period_quantity": period_quantity,
                "module_list": [
                    {"module_code": 'Region', "config": f"Region:{region_id}"},
                    {"module_code": 'Spec', "config": f"Spec:{specs['Spec']}"}
                ]
            }
            logger.print_block(f"BSS GetSubscriptionPrice Payload - [{instance_id}]", request_log_payload)

            # 4. 发送询价请求
            request = bss_open_api_20171214_models.GetSubscriptionPriceRequest(
                subscription_type='Subscription',
                service_period_unit=period_unit,
                product_code='pconn',
                order_type='Renewal',
                instance_id=instance_id,
                module_list=[module_region, module_spec],
                region=region_id,
                service_period_quantity=period_quantity
            )

            response = bss_client.get_subscription_price_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            logger.print_block(f"BSS Raw Response - [{instance_id}]", res_body)

            is_success = res_body.get('Success')
            req_id = res_body.get('RequestId', 'N/A')

            if is_success:
                data = res_body.get('Data', {})
                trade_price = float(data.get('TradePrice', 0.0))
                original_price = float(data.get('OriginalPrice', 0.0))
                currency = data.get('Currency', 'CNY')  # 核心：国际站将自动返回 USD
                return trade_price, original_price, currency, specs
            else:
                msg = res_body.get('Message', 'API 询价失败')
                code = res_body.get('Code', '')
                logger.warning(f"❌ [API 拦截透出] 阿里云 BSS 网关拒绝: [{code}] {msg}")

                # 探针启动：如果组件不匹配，调用 DescribePricingModule 获取底层计费字典
                if 'INVALID_COMPONENT' in code:
                    logger.warning(f"🔍 [探针启动] 正在调用 DescribePricingModule 获取 pconn 底层计费字典...")
                    try:
                        probe_req = bss_open_api_20171214_models.DescribePricingModuleRequest(
                            subscription_type='Subscription',
                            product_code='pconn',
                            product_type=''
                        )
                        probe_res = bss_client.describe_pricing_module_with_options(probe_req,
                                                                                    util_models.RuntimeOptions())
                        probe_map = probe_res.body.to_map()
                        logger.print_block("BSS 要求的全量组件架构字典", probe_map, level=logging.ERROR)
                    except Exception as probe_e:
                        logger.error(f"❌ [探针错误] 获取失败: {str(probe_e)}")

                return -1.0, -1.0, f"BSS拒绝: [{code}] {msg} (RequestId: {req_id})", {}

        except Exception as e:
            logger.error(f"❌ [询价异常] {str(e)}")
            return -1.0, -1.0, f"技术异常: {str(e)}", {}

    @staticmethod
    def main():
        logger.print_step("PROGRAM START: PHYSICAL CONNECTION (PCONN) RENEWAL PRICE QUERY (GLOBAL EDITION)")

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

        session_to_section = AliyunPconnRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunPconnRenewalPriceQuery.load_duration_mapping()

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

                expire_time_norm = expire_time_raw.replace('/', '-')

                if product_code == 'pconn' or instance_id.startswith('pc-'):
                    if account:
                        if expire_time_norm.startswith(next_month_str):
                            if account not in instances_by_account:
                                instances_by_account[account] = []
                            instances_by_account[account].append(row)
                            filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【物理专线(pconn) + 下月到期】条件的实例。")

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
            logger.info(f"\n▶▶ 🏁 账号集群: [{account}] | 物理专线总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunPconnRenewalPriceQuery.get_sts_credentials_by_role(section)

                # VPC 属于业务接口，全球通用，维持原样
                vpc_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token,
                    endpoint='vpc.aliyuncs.com'
                )
                vpc_client = Vpc20160428Client(vpc_config)

                # ========================== 核心修复 ==========================
                # BSS 属于财务接口，对国际站(alibabacloud)必须指定 ap-southeast-1 节点
                bss_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token,
                    endpoint='business.ap-southeast-1.aliyuncs.com'
                )
                bss_client = BssOpenApi20171214Client(bss_config)
                # ==============================================================

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

                trade_price, original_price, status_or_currency, applied_specs = AliyunPconnRenewalPriceQuery.query_pconn_price_via_bss(
                    bss_client, vpc_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': item.get('产品代码', 'pconn'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    total_success += 1
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency

                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0

                    spec_desc = applied_specs.get('Spec', '')
                    out_row['描述'] = f"规格:{spec_desc}"

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
    AliyunPconnRenewalPriceQuery.main()
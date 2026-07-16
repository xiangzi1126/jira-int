# -*- coding: utf-8 -*-
"""
阿里云包月 云企业网-带宽包 (cbn) 开放 API 真实续费开销询价工具（国际/国内通用版）
功能：
1. 引入官方原生 cbn20170912 强类型客户端与 DescribeCenBandwidthPackagesRequest 接口；
2. 实时精准拉取带宽包的真实带宽值 (Bandwidth)、区域A (GeographicRegionAId)、区域B (GeographicRegionBId)；
3. 【核心修正】：彻底删除所有地域名的默认值盲猜，100% 忠实透传原生 API 数据；
4. 【核心修正】：严格遵循 BSS OpenAPI 针对 CBN 的小写 bandwidth 传参规范；
5. 【动态传参修正】：摒弃写死的 ProductType，严格从 aliyun_renewal_bill 账单中动态提取真实的“产品类型”和“产品代码”进行询价。
6. 【财务节点修正】：BSS 财务中心 Endpoint 已替换为国际站专属节点 (ap-southeast-1)。
7. 【极具可视化的探针全显】：强制以高可读性格式在控制台全量输出属性字典与询价 Payload。
8. 【时间筛选增强】：自动计算并严格过滤出“下月到期”的资源进行精准询价。
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

# 引入官方 cbn 强类型产品依赖包
from alibabacloud_cbn20170912.client import Client as Cbn20170912Client
from alibabacloud_cbn20170912 import models as cbn_20170912_models

from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/cbn_renewal_price.log'))


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

        self.logger = logging.getLogger('cbn_renewal_visual')
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
class AliyunCbnRenewalPriceQuery:
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
        master_ak_id, master_ak_secret = AliyunCbnRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunCbnRenewalPriceQuery.sanitize_session_name(raw_session_name)
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
    def fetch_cbn_physical_specs(cbn_client: Cbn20170912Client, instance_id: str) -> Dict[str, Any]:
        """使用官方原生强类型请求精准提取云企业网带宽包的计费属性，绝不盲猜默认值"""
        logger.info(f"🔍 [CBN Probe] 正在调用强类型接口检索云企业网带宽包明细: {instance_id}")

        try:
            request = cbn_20170912_models.DescribeCenBandwidthPackagesRequest(
                filter=[
                    cbn_20170912_models.DescribeCenBandwidthPackagesRequestFilter(
                        key="CenBandwidthPackageId",
                        value=[instance_id]
                    )
                ]
            )

            response = cbn_client.describe_cen_bandwidth_packages_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            logger.print_block(f"CBN DescribeCenBandwidthPackages Result - [{instance_id}]", res_body)

            bwp_list = res_body.get('CenBandwidthPackages', {}).get('CenBandwidthPackage', [])
            if not bwp_list:
                raise ValueError("CBN 未能返回此云企业网带宽包记录，资源可能已释放或没有权限访问。")

            bwp_info = bwp_list[0]

            # 提取原生态属性，坚决不用默认值盲猜，直接透传真实 API 返回
            raw_region_a = str(bwp_info.get('GeographicRegionAId', '')).strip()
            raw_region_b = str(bwp_info.get('GeographicRegionBId', '')).strip()
            bandwidth = str(bwp_info.get('Bandwidth', '')).strip()

            # 组装最终计费所需的指纹参数
            specs = {
                'Bandwidth': bandwidth,
                'RegionA': raw_region_a,
                'RegionB': raw_region_b
            }
            logger.info(f"👉 [CBN Probe] 探测成功 -> 提取出的原生计费规格指纹: {specs}")
            return specs
        except Exception as e:
            logger.error(f"❌ [CBN Probe] 探测崩溃: {str(e)}")
            raise

    @staticmethod
    def query_cbn_price_via_bss(
            bss_client: BssOpenApi20171214Client,
            cbn_client: Cbn20170912Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, Dict[str, Any]]:
        """组合多组件进行云企业网带宽包询价 (动态获取账单中的真实 ProductType，并强制使用小写 bandwidth)"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', '').strip()

        # 动态从 CSV 账单行中读取真实的产品代码和产品类型
        product_code = item.get('产品代码', 'cbn').strip()
        product_type = item.get('产品类型', '').strip()

        try:
            # 1. 获取物理计费指纹
            specs = AliyunCbnRenewalPriceQuery.fetch_cbn_physical_specs(cbn_client, instance_id)

            # 2. 按照要求对齐 ConfigList 组装（强制使用小写的 bandwidth）
            module_bandwidth = bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='bandwidth',
                config=f"bandwidth:{specs['Bandwidth']},RegionA:{specs['RegionA']},RegionB:{specs['RegionB']}"
            )

            # 3. 记录日志信息
            request_log_payload = {
                "subscription_type": 'Subscription',
                "service_period_unit": period_unit,
                "product_code": product_code,
                "product_type": product_type,
                "order_type": 'Renewal',
                "instance_id": instance_id,
                "region": region_id,
                "service_period_quantity": period_quantity,
                "module_list": [
                    {
                        "module_code": 'bandwidth',
                        "config": f"bandwidth:{specs['Bandwidth']},RegionA:{specs['RegionA']},RegionB:{specs['RegionB']}"
                    }
                ]
            }
            logger.print_block(f"BSS GetSubscriptionPrice Payload - [{instance_id}]", request_log_payload)

            # 4. 发送询价请求
            request = bss_open_api_20171214_models.GetSubscriptionPriceRequest(
                subscription_type='Subscription',
                service_period_unit=period_unit,
                product_code=product_code,
                product_type=product_type,
                order_type='Renewal',
                instance_id=instance_id,
                module_list=[module_bandwidth],
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
                currency = data.get('Currency', 'CNY')
                return trade_price, original_price, currency, specs
            else:
                msg = res_body.get('Message', 'API 询价失败')
                code = res_body.get('Code', '')
                logger.warning(f"❌ [API 拦截透出] 阿里云 BSS 网关拒绝: [{code}] {msg}")
                return -1.0, -1.0, f"BSS拒绝: [{code}] {msg} (RequestId: {req_id})", {}

        except Exception as e:
            logger.error(f"❌ [询价异常] {str(e)}")
            return -1.0, -1.0, f"技术异常: {str(e)}", {}

    @staticmethod
    def main():
        logger.print_step("PROGRAM START: CEN BANDWIDTH PACKAGE (CBN) RENEWAL PRICE QUERY (GLOBAL EDITION)")

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

        session_to_section = AliyunCbnRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunCbnRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                instance_id = row.get('资源id', '').strip()
                product_code = row.get('产品代码', '').lower().strip()
                product_type = row.get('产品类型', '').lower().strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()

                expire_time_norm = expire_time_raw.replace('/', '-')

                # 增加健壮性：兼容各种国际站和国内站常见的 cbn 产品类型标识
                if product_code == 'cbn' or product_type in ['cbn_bwp_pre_mkt', 'cenbwp',
                                                             'cbn_bwp_intl'] or instance_id.startswith('cenbwp-'):
                    if account:
                        if expire_time_norm.startswith(next_month_str):
                            if account not in instances_by_account:
                                instances_by_account[account] = []
                            instances_by_account[account].append(row)
                            filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【云企业网带宽包 + 下月到期】条件的实例。")

        # ⭐ 严格对齐表头
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
            logger.info(f"\n▶▶ 🏁 账号集群: [{account}] | 待处理云企业网带宽包总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunCbnRenewalPriceQuery.get_sts_credentials_by_role(section)

                # CBN 资源型接口：维持原有官方统一点
                cbn_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token,
                    endpoint='cbn.aliyuncs.com'
                )
                cbn_client = Cbn20170912Client(cbn_config)

                # BSS 财务型接口：针对国际站(alibabacloud) 必须指定 ap-southeast-1 节点
                bss_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token,
                    endpoint='business.ap-southeast-1.aliyuncs.com'
                )
                bss_client = BssOpenApi20171214Client(bss_config)

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

                trade_price, original_price, status_or_currency, applied_specs = AliyunCbnRenewalPriceQuery.query_cbn_price_via_bss(
                    bss_client, cbn_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': item.get('产品代码', 'cbn'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    total_success += 1
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency

                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0

                    bw_desc = applied_specs.get('Bandwidth', '')
                    r_a = applied_specs.get('RegionA', '')
                    r_b = applied_specs.get('RegionB', '')
                    out_row['描述'] = f"带宽:{bw_desc}Mbps | 互通:{r_a}<->{r_b}"

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
    AliyunCbnRenewalPriceQuery.main()
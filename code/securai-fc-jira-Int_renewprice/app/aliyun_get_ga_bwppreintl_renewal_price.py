# -*- coding: utf-8 -*-
"""
阿里云包月 全球加速-基础带宽包 (ga_bwppreintl_public_intl) 专属续费询价工具
功能：1. 【严格过滤修复】：正确提取【产品类型】列，强制其必须等于 ga_bwppreintl_public_intl；
      2. 【专属探针】：使用 DescribeBandwidthPackage 获取真实的带宽值；
      3. 【完美组装】：100% 遵照阿里工单的 Payload 结构，并将 type 组件强制写死为 Enhanced。
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
from alibabacloud_ga20191120.client import Client as Ga20191120Client
from alibabacloud_ga20191120 import models as ga_20191120_models

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/ga_bwppreintl_renewal_price.log'))


def init_logger() -> logging.Logger:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_format = logging.Formatter('%(asctime)s [%(levelname)s] [Line %(lineno)d] - %(message)s')
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(log_format)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_format)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = init_logger()


class AliyunGaBwpRenewalPriceQuery:
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
        master_ak_id, master_ak_secret = AliyunGaBwpRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunGaBwpRenewalPriceQuery.sanitize_session_name(raw_session_name)
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
        return mapping

    @staticmethod
    def load_duration_mapping() -> Dict[str, float]:
        sbu_csv_path = os.path.join(DATA_DIR, 'jira_get_renewal_sbu_issues.csv')
        duration_map = {}
        if not os.path.exists(sbu_csv_path):
            logger.warning(f"⚠️ 警告: 未找到 Jira 周期工单文件 {sbu_csv_path}，默认按 1.0 个月询价。")
            return duration_map

        try:
            with open(sbu_csv_path, 'r', encoding='utf-8') as f:
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
        return duration_map

    @staticmethod
    def query_bwp_price_native(
            ga_client: Ga20191120Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, Dict[str, Any]]:
        """纯粹处理基础带宽包实例的提取与询价"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', 'cn-hangzhou').strip() or 'cn-hangzhou'
        commodity_code = 'ga_bwppreintl_public_intl'

        specs = {'CommodityCode': commodity_code}
        bwp_map = {}

        # ========================================================
        # 1. 探针：调用 DescribeBandwidthPackage 获取真实带宽大小
        # ========================================================
        try:
            describe_bwp_req = ga_20191120_models.DescribeBandwidthPackageRequest(
                bandwidth_package_id=instance_id,
                region_id=region_id
            )
            bwp_res = ga_client.describe_bandwidth_package_with_options(describe_bwp_req, util_models.RuntimeOptions())
            bwp_map = bwp_res.body.to_map() if hasattr(bwp_res.body, 'to_map') else dict(bwp_res.body)

            # 动态提取真实带宽与类型
            specs['Bandwidth'] = str(bwp_map.get('Bandwidth', '5'))
            specs['Type'] = str(bwp_map.get('Type', 'Basic'))  # 通常是 Basic
            specs['State'] = str(bwp_map.get('State', 'N/A'))
            logger.info(
                f"   [实例提取成功] ID: {instance_id} | 带宽大小: {specs['Bandwidth']} Mbps | 类型: {specs['Type']}")

        except Exception as bwp_e:
            logger.error(f"   [探针崩溃] DescribeBandwidthPackage 失败: {str(bwp_e)}")

        # ========================================================
        # 2. 严格按工单组装官方计费组件并发起询价
        # ========================================================
        logger.info(f"   [询价启动] 正在为 {commodity_code} 组装基础带宽包计费组件...")
        try:
            components_list = []

            # 1. ord_time (时长)
            ord_time_str = f"{period_quantity}:{period_unit}"
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='ord_time',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='ord_time',
                                                                                                       value=ord_time_str)]
            ))

            # 2. bandwidth (带宽值，动态取自实例探针)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='bandwidth',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='bandwidth',
                                                                                                       value=specs.get(
                                                                                                           'Bandwidth',
                                                                                                           '5'))]
            ))

            # 3. BandwidthPackageType (通常为 Basic)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='BandwidthPackageType',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(
                    code='BandwidthPackageType', value=specs.get('Type', 'Basic'))]
            ))

            # 4. type (强制定制：写死为 Enhanced)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='type',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='type',
                                                                                                       value='Enhanced')]
            ))

            # 构建 Order 结构
            order = ga_20191120_models.DescribeCommodityPriceRequestOrders(
                commodity_code=commodity_code,
                order_type='RENEW',
                charge_type=period_unit,
                pricing_cycle=str(period_quantity),
                duration=period_quantity,
                quantity=1,
                components=components_list
            )

            describe_commodity_price_request = ga_20191120_models.DescribeCommodityPriceRequest(
                region_id=region_id,
                orders=[order]
            )

            try:
                debug_payload = json.dumps(describe_commodity_price_request.to_map(), ensure_ascii=False, indent=4)
                logger.debug(f"   [排错日志 - 询价 Payload] 🔍 发往 DescribeCommodityPrice 的参数:\n{debug_payload}")
            except Exception:
                pass

            price_res = ga_client.describe_commodity_price_with_options(describe_commodity_price_request,
                                                                        util_models.RuntimeOptions())
            res_body = price_res.body.to_map() if hasattr(price_res.body, 'to_map') else dict(price_res.body)

            try:
                debug_res_body = json.dumps(res_body, ensure_ascii=False, indent=4)
                logger.debug(f"   [排错日志 - 询价返回原始报文] 🚨 完整网关响应:\n{debug_res_body}")
            except Exception:
                pass

            # ========================================================
            # 提取价格：直接提取最外层 TradePrice
            # ========================================================
            if 'TradePrice' in res_body:
                trade_price = float(res_body.get('TradePrice', 0.0))
                original_price = float(res_body.get('OriginalPrice', 0.0))
                currency = res_body.get('Currency', 'CNY')

                logger.info(
                    f"   └─ ✅ 询价完美成功! ID: {instance_id} | 原价: {original_price} | 最终价: {trade_price} {currency}")
                return trade_price, original_price, currency, specs
            else:
                msg = res_body.get('Message', '')
                code = res_body.get('Code', '')

                if not msg:
                    error_data = res_body.get('Data', {})
                    if isinstance(error_data, dict):
                        msg = error_data.get('Message', error_data.get('errorMsg', 'API 询价失败 (详见原始报文)'))
                        code = error_data.get('Code', error_data.get('errorCode', ''))

                logger.warning(f"   [API 拦截透出] 基础带宽包询价网关拒绝: [{code}] {msg}")
                return -1.0, -1.0, f"网关拒绝: [{code}] {msg}", specs

        except Exception as e:
            error_msg = str(e)
            logger.error(f"   └─ ❌ API拒绝: ID: {instance_id} | 技术异常: {error_msg}")
            short_msg = "组件校验失败" if "Missing" in error_msg or "Invalid" in error_msg else "API报错拒绝"
            return -1.0, -1.0, f"受阻: {short_msg}", specs

    @staticmethod
    def main():
        logger.info("=" * 100)
        logger.info("  🚀 阿里云全球加速 - 基础带宽包 (ga_bwppreintl_public_intl) 专属询价工具")
        logger.info("=" * 100)

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        first_day_of_next_month = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month_str = first_day_of_next_month.strftime("%Y-%m")
        current_month_str = today.strftime("%Y-%m")
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        logger.info(f"📅 全局筛选条件: 仅处理到期时间在 [{next_month_str}] 期间的资源")

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置到期数据清单 {input_csv_path}。")
            sys.exit(1)

        session_to_section = AliyunGaBwpRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunGaBwpRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                instance_id = row.get('资源id', '').strip()

                # ⭐ 核心修复：同时提取【产品代码】和【产品类型】以防列名偏差
                product_code = row.get('产品代码', '').lower().strip()
                product_type = row.get('产品类型', '').lower().strip()

                account = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()
                expire_time_norm = expire_time_raw.replace('/', '-')

                # ========================================================
                # ⭐ 严格过滤：强制检查产品代码或产品类型是否等于 ga_bwppreintl_public_intl
                # ========================================================
                if product_code == 'ga_bwppreintl_public_intl' or product_type == 'ga_bwppreintl_public_intl':
                    if account:
                        if expire_time_norm.startswith(next_month_str):
                            if account not in instances_by_account:
                                instances_by_account[account] = []
                            instances_by_account[account].append(row)
                            filtered_count += 1

        logger.info(f"📊 基础过滤完成：扫描 {total_count} 行，筛选出 {filtered_count} 个符合【基础带宽包】条件的实例。")

        output_fields = [
            '资源id', '资源所属账号', '资源到期时间', '产品代码',
            '描述', '原价', '折扣', '货币单位', '最终价'
        ]

        file_exists = os.path.exists(output_csv_path)
        with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            if not file_exists:
                writer.writeheader()

        for account, items in instances_by_account.items():
            logger.info(f"\n🏁 账号集群: [{account}] | 带宽包资源总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunGaBwpRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token
                )
                open_config.endpoint = 'ga.cn-hangzhou.aliyuncs.com'
                ga_client = Ga20191120Client(open_config)

            except Exception as e:
                logger.error(f"❌ 初始化失败: {str(e)}")
                continue

            processed_rows = []
            for item in items:
                instance_id = item.get('资源id', '').strip()

                duration_val = duration_map.get(instance_id, 1.0)
                period_unit, period_quantity = ('Year', 1) if duration_val == 12.0 else ('Month', (
                    int(duration_val) if duration_val > 0 else 1))

                trade_price, original_price, status_or_currency, applied_specs = AliyunGaBwpRenewalPriceQuery.query_bwp_price_native(
                    ga_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': applied_specs.get('CommodityCode', 'ga_bwppreintl_public_intl'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency
                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0

                    bwp_size = applied_specs.get('Bandwidth', 'N/A')
                    out_row['描述'] = f"基础带宽包|带宽:{bwp_size}Mbps|属性:Enhanced"
                else:
                    out_row['描述'] = status_or_currency

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=output_fields)
                writer.writerows(processed_rows)

        logger.info(f"\n✅ 基础带宽包处理完毕。")


if __name__ == '__main__':
    AliyunGaBwpRenewalPriceQuery.main()
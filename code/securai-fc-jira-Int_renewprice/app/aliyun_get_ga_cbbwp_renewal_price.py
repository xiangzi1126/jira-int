# -*- coding: utf-8 -*-
"""
阿里云包月 全球加速-跨境带宽包 (ga_cbbwp_public_intl) 专属续费估价工具
功能：1. 【严格过滤】：提取【产品类型】为 ga_cbbwp_public_intl 的下月到期资源；
      2. 【专属探针】：调用 DescribeBandwidthPackage 获取真实带宽 (bandwidth)；
      3. 【硬核组装】：严格遵照跨境带宽包的 Payload 要求，强制写入 CrossDomain、
         China-mainland、Global 以及 OrderType=BUY 等定制参数。
"""
import os
import sys
import csv
import re
import configparser
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple

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
# 修改为跨境带宽包专属日志
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/ga_cbbwp_renewal_price.log'))


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


class AliyunGaCbbwpRenewalPriceQuery:
    @staticmethod
    def sanitize_session_name(name: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', name)
        return sanitized[:64] if len(sanitized) >= 2 else "STS_Session"

    @staticmethod
    def get_master_credentials() -> Tuple[str, str]:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        try:
            return config.get('aliyun', 'access_key_id'), config.get('aliyun', 'access_key_secret')
        except Exception as e:
            logger.error(f"❌ 主账号配置读取失败: {str(e)}")
            raise

    @staticmethod
    def get_sts_credentials_by_role(role_section: str) -> Any:
        master_ak_id, master_ak_secret = AliyunGaCbbwpRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunGaCbbwpRenewalPriceQuery.sanitize_session_name(raw_session_name)
        sts_config = open_api_models.Config(access_key_id=master_ak_id, access_key_secret=master_ak_secret, endpoint='sts.aliyuncs.com')
        sts_client = StsClient(sts_config)

        assume_role_request = AssumeRoleRequest(role_arn=role_arn, role_session_name=api_safe_session_name, duration_seconds=3600)
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
        # 从 Jira 提取的 SBU 工单中获取续费时长
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
    def query_cbbwp_price_native(
            ga_client: Ga20191120Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, Dict[str, Any]]:
        """处理 GA 跨境带宽包专属的提取与询价"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', 'cn-hangzhou').strip() or 'cn-hangzhou'
        commodity_code = 'ga_cbbwp_public_intl'

        specs = {'CommodityCode': commodity_code}

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

            # 动态提取真实带宽
            specs['Bandwidth'] = str(bwp_map.get('Bandwidth', '5'))
            logger.info(f"   [探针成功] ID: {instance_id} | 探测到真实带宽: {specs['Bandwidth']} Mbps")

        except Exception as bwp_e:
            logger.error(f"   [探针崩溃] DescribeBandwidthPackage 失败: {str(bwp_e)}，默认回退带宽为 5Mbps")
            specs['Bandwidth'] = '5'

        # ========================================================
        # 2. 严格按工单组装 跨境带宽包 官方计费组件并发起询价
        # ========================================================
        logger.info(f"   [询价启动] 正在为 {commodity_code} 组装跨境计费组件...")
        try:
            components_list = []

            # 1. flow_out (写死)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='flow_out',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='flow_out', value='0')]
            ))

            # 2. bandwidth (来自探针)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='bandwidth',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='bandwidth', value=specs.get('Bandwidth', '5'))]
            ))

            # 3. BandwidthPackageType (写死)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='BandwidthPackageType',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='BandwidthPackageType', value='CrossDomain')]
            ))

            # 4. cbn_geographic_region_id_A (写死)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='cbn_geographic_region_id_A',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='cbn_geographic_region_id_A', value='China-mainland')]
            ))

            # 5. ord_time (来自工单时长)
            ord_time_str = f"{period_quantity}:{period_unit}"
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='ord_time',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='ord_time', value=ord_time_str)]
            ))

            # 6. cbn_geographic_region_id_B (写死)
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='cbn_geographic_region_id_B',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='cbn_geographic_region_id_B', value='Global')]
            ))

            # 构建 Order 结构 (按照你的需求写死 BUY 和 PREPAY)
            order = ga_20191120_models.DescribeCommodityPriceRequestOrders(
                commodity_code=commodity_code,
                order_type='BUY',
                charge_type='PREPAY',
                pricing_cycle=period_unit,
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

            price_res = ga_client.describe_commodity_price_with_options(describe_commodity_price_request, util_models.RuntimeOptions())
            res_body = price_res.body.to_map() if hasattr(price_res.body, 'to_map') else dict(price_res.body)

            # 提取价格：最外层 TradePrice
            if 'TradePrice' in res_body:
                trade_price = float(res_body.get('TradePrice', 0.0))
                original_price = float(res_body.get('OriginalPrice', 0.0))
                currency = res_body.get('Currency', 'CNY')

                logger.info(f"   └─ ✅ 询价完美成功! ID: {instance_id} | 原价: {original_price} | 最终价: {trade_price} {currency}")
                return trade_price, original_price, currency, specs
            else:
                msg = res_body.get('Message', '')
                code = res_body.get('Code', '')

                if not msg:
                    error_data = res_body.get('Data', {})
                    if isinstance(error_data, dict):
                        msg = error_data.get('Message', error_data.get('errorMsg', 'API 询价失败 (详见原始报文)'))
                        code = error_data.get('Code', error_data.get('errorCode', ''))

                logger.warning(f"   [API 拦截透出] 跨境带宽包询价网关拒绝: [{code}] {msg}")
                return -1.0, -1.0, f"网关拒绝: [{code}] {msg}", specs

        except Exception as e:
            error_msg = str(e)
            logger.error(f"   └─ ❌ API拒绝: ID: {instance_id} | 技术异常: {error_msg}")
            short_msg = "组件校验失败" if "Missing" in error_msg or "Invalid" in error_msg else "API报错拒绝"
            return -1.0, -1.0, f"受阻: {short_msg}", specs


    @staticmethod
    def main():
        logger.info("=" * 100)
        logger.info("  🚀 阿里云全球加速 - 跨境带宽包 (ga_cbbwp_public_intl) 专属询价工具")
        logger.info("=" * 100)

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        first_day_of_next_month = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month_str = first_day_of_next_month.strftime("%Y-%m")
        current_month_str = today.strftime("%Y-%m")
        # 输出统一追加到当月价格表
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置到期数据清单 {input_csv_path}。")
            sys.exit(1)

        session_to_section = AliyunGaCbbwpRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunGaCbbwpRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                # 核心筛选：只处理跨境带宽包 (ga_cbbwp_public_intl)
                product_code = row.get('产品代码', '').lower().strip()
                product_type = row.get('产品类型', '').lower().strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_norm = row.get('资源到期时间', '').strip().replace('/', '-')

                if product_code == 'ga_cbbwp_public_intl' or product_type == 'ga_cbbwp_public_intl':
                    if account and expire_time_norm.startswith(next_month_str):
                        if account not in instances_by_account:
                            instances_by_account[account] = []
                        instances_by_account[account].append(row)
                        filtered_count += 1

        logger.info(f"📊 基础过滤完成：扫描 {total_count} 行，筛选出 {filtered_count} 个符合【跨境带宽包】条件的实例。")

        output_fields = ['资源id', '资源所属账号', '资源到期时间', '产品代码', '描述', '原价', '折扣', '货币单位', '最终价']
        file_exists = os.path.exists(output_csv_path)
        with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            if not file_exists:
                writer.writeheader()

        for account, items in instances_by_account.items():
            logger.info(f"\n🏁 账号集群: [{account}] | 跨境带宽包资源数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunGaCbbwpRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token,
                    endpoint='ga.cn-hangzhou.aliyuncs.com'
                )
                ga_client = Ga20191120Client(open_config)
            except Exception as e:
                logger.error(f"❌ 初始化STS失败: {str(e)}")
                continue

            processed_rows = []
            for item in items:
                instance_id = item.get('资源id', '').strip()
                duration_val = duration_map.get(instance_id, 1.0)
                period_unit, period_quantity = ('Year', 1) if duration_val == 12.0 else ('Month', (int(duration_val) if duration_val > 0 else 1))

                trade_price, original_price, status_or_currency, applied_specs = AliyunGaCbbwpRenewalPriceQuery.query_cbbwp_price_native(
                    ga_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': applied_specs.get('CommodityCode', 'ga_cbbwp_public_intl'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency
                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0
                    bwp_size = applied_specs.get('Bandwidth', 'N/A')
                    out_row['描述'] = f"跨境带宽包|带宽:{bwp_size}Mbps|属性:CrossDomain"
                else:
                    out_row['描述'] = status_or_currency

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=output_fields)
                writer.writerows(processed_rows)

        logger.info(f"\n✅ 跨境带宽包处理完毕。")


if __name__ == '__main__':
    AliyunGaCbbwpRenewalPriceQuery.main()
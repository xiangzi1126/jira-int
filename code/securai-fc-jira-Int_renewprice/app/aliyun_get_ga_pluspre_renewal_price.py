# -*- coding: utf-8 -*-
"""
阿里云包月 全球加速 (GA) 专属 API 真实续费开销询价工具
功能：1. 【极简架构】：100% 专一处理 ga- 加速器实例；
      2. 【完美组装】：基于阿里官方工单，精准映射 6 大 Component 询价；
      3. 【终极解析修复】：根据真实报文证据，取消错误的 Success 字段校验，直接提取平铺的最外层价格。
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
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/ga_renewal_price.log'))


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


class AliyunGaRenewalPriceQuery:
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
        master_ak_id, master_ak_secret = AliyunGaRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunGaRenewalPriceQuery.sanitize_session_name(raw_session_name)
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
    def query_ga_price_native(
            ga_client: Ga20191120Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, Dict[str, Any]]:
        """纯粹处理 ga- 实例的提取与询价"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', 'cn-hangzhou').strip() or 'cn-hangzhou'
        commodity_code = 'ga_pluspre_public_intl'

        specs = {'CommodityCode': commodity_code}
        acc_map = {}

        # ========================================================
        # 1. 查询实例详情
        # ========================================================
        try:
            describe_accelerator_request = ga_20191120_models.DescribeAcceleratorRequest(
                accelerator_id=instance_id,
                region_id=region_id
            )
            acc_res = ga_client.describe_accelerator_with_options(describe_accelerator_request,
                                                                  util_models.RuntimeOptions())
            acc_map = acc_res.body.to_map() if hasattr(acc_res.body, 'to_map') else dict(acc_res.body)

            specs['Spec'] = str(acc_map.get('Spec', '1'))
            specs['BandwidthBillingType'] = str(acc_map.get('BandwidthBillingType', 'BandwidthPackage'))
            specs['State'] = str(acc_map.get('State', 'N/A'))
            logger.info(
                f"   [实例提取成功] ID: {instance_id} | 规格: {specs['Spec']} | 计费类型: {specs['BandwidthBillingType']}")

        except Exception as acc_e:
            logger.error(f"   [实例探针崩溃] DescribeAccelerator 失败: {str(acc_e)}")

        # ========================================================
        # 2. 组装官方计费组件并发起询价
        # ========================================================
        logger.info(f"   [询价启动] 正在为 {commodity_code} 组装官方计费组件...")
        try:
            components_list = []

            # 组装 GA 专属的 6 大组件
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='ga_bandwidth_fee',
                properties=[
                    ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='ga_bandwidth_fee',
                                                                                               value=specs.get(
                                                                                                   'BandwidthBillingType',
                                                                                                   'BandwidthPackage'))]
            ))
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='spec',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='spec',
                                                                                                       value=specs.get(
                                                                                                           'Spec',
                                                                                                           '1'))]
            ))
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='instance',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='instance',
                                                                                                       value='instance_fee')]
            ))
            ord_time_str = f"{period_quantity}:{period_unit}"
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='ord_time',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='ord_time',
                                                                                                       value=ord_time_str)]
            ))
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='accelerate_ip_type',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(
                    code='accelerate_ip_type', value='eip')]
            ))
            components_list.append(ga_20191120_models.DescribeCommodityPriceRequestOrdersComponents(
                component_code='type',
                properties=[ga_20191120_models.DescribeCommodityPriceRequestOrdersComponentsProperties(code='type',
                                                                                                       value='standard')]
            ))

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

            price_res = ga_client.describe_commodity_price_with_options(describe_commodity_price_request,
                                                                        util_models.RuntimeOptions())
            res_body = price_res.body.to_map() if hasattr(price_res.body, 'to_map') else dict(price_res.body)

            # 保留探针：输出美化的报文供随时抽查
            try:
                debug_res_body = json.dumps(res_body, ensure_ascii=False, indent=4)
                logger.debug(f"   [排错日志 - 询价返回原始报文] 🚨 完整网关响应:\n{debug_res_body}")
            except Exception:
                pass

            # ========================================================
            # ⭐ 核心解析修复：不再校验 Success 字段，直接基于 TradePrice 判定
            # ========================================================
            if 'TradePrice' in res_body:
                trade_price = float(res_body.get('TradePrice', 0.0))
                original_price = float(res_body.get('OriginalPrice', 0.0))
                currency = res_body.get('Currency', 'CNY')

                logger.info(
                    f"   └─ ✅ 询价完美成功! ID: {instance_id} | 原价: {original_price} | 最终价: {trade_price} {currency}")
                return trade_price, original_price, currency, specs
            else:
                # 尝试抓取更深层的错误信息
                msg = res_body.get('Message', '')
                code = res_body.get('Code', '')

                if not msg:
                    error_data = res_body.get('Data', {})
                    if isinstance(error_data, dict):
                        msg = error_data.get('Message', error_data.get('errorMsg', 'API 询价失败 (详见原始报文)'))
                        code = error_data.get('Code', error_data.get('errorCode', ''))

                logger.warning(f"   [API 拦截透出] GA 询价网关拒绝: [{code}] {msg}")
                return -1.0, -1.0, f"GA拒绝: [{code}] {msg}", specs

        except Exception as e:
            error_msg = str(e)
            logger.error(f"   └─ ❌ API拒绝: ID: {instance_id} | 技术异常: {error_msg}")
            short_msg = "组件校验失败" if "Missing" in error_msg or "Invalid" in error_msg else "API报错拒绝"
            return -1.0, -1.0, f"受阻: {short_msg}", specs

    @staticmethod
    def main():
        logger.info("=" * 100)
        logger.info("  🚀 阿里云全球加速 (GA) 续费询价工具 (终极成功版)")
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

        session_to_section = AliyunGaRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunGaRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                instance_id = row.get('资源id', '').strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()
                expire_time_norm = expire_time_raw.replace('/', '-')

                # ========================================================
                # ⭐ 绝对过滤：只认 ga- 开头的实例，彻底无视 gbwp- / cbwp-
                # ========================================================
                if instance_id.startswith('ga-'):
                    if account:
                        if expire_time_norm.startswith(next_month_str):
                            if account not in instances_by_account:
                                instances_by_account[account] = []
                            instances_by_account[account].append(row)
                            filtered_count += 1

        logger.info(f"📊 基础过滤完成：扫描 {total_count} 行，筛选出 {filtered_count} 个符合【ga- 加速器】条件的实例。")

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
            logger.info(f"\n🏁 账号集群: [{account}] | 加速器资源总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunGaRenewalPriceQuery.get_sts_credentials_by_role(section)
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

                trade_price, original_price, status_or_currency, applied_specs = AliyunGaRenewalPriceQuery.query_ga_price_native(
                    ga_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': applied_specs.get('CommodityCode', 'ga_pluspre_public_intl'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency
                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0

                    spec_desc = applied_specs.get('Spec', 'N/A')
                    out_row['描述'] = f"商品:{applied_specs.get('CommodityCode', '')}|规格:{spec_desc}"
                else:
                    out_row['描述'] = status_or_currency

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=output_fields)
                writer.writerows(processed_rows)

        logger.info(f"\n✅ 执行完毕。")


if __name__ == '__main__':
    AliyunGaRenewalPriceQuery.main()
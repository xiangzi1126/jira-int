# -*- coding: utf-8 -*-
"""
阿里云 数字证书管理服务 (CAS / SSL 证书) 真实续费开销询价工具
功能：
1. 完美对接 STS 认证逻辑（主账号 AK 换取子账号临时令牌）。
2. 【历史账单穿透提取】：根据续费周期 (1年)，自动穿透读取 [到期时间-去年同月] 的 aliyun_bill 提取配置。
3. 【SKU 防撞墙修正】：修复了 Starter(免费版) 与 All(通配符) 组合导致 PRICING_PLAN_RESULT_NOT_FOUND 的计费冲突，强制将通配符及第三方证书映射为 Base(付费标准版)。
4. 【CSV 账单逆向解析引擎】：精准提取 Brand, CertType, DomainType 并拼装 BSS 强制字典。
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

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/cas_renewal_price.log'))


def init_logger() -> logging.Logger:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(log_format)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_format)

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = init_logger()


class VisualLogger:
    """极具可视化排版的详细日志系统"""

    @staticmethod
    def print_panel(title: str, data: Any, level: str = 'info'):
        border = "═" * 80
        msg = f"\n{border}\n ❖ {title} ❖\n{border}\n"
        if isinstance(data, (dict, list)):
            msg += json.dumps(data, ensure_ascii=False, indent=4)
        else:
            msg += str(data)
        msg += f"\n{border}"
        if level == 'info':
            logger.info(msg)
        elif level == 'error':
            logger.error(msg)
        else:
            logger.debug(msg)


class AliyunCasRenewalPriceQuery:
    # 历史账单缓存池
    _historical_bills_cache: Dict[str, Dict[str, Dict]] = {}

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
        master_ak_id, master_ak_secret = AliyunCasRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunCasRenewalPriceQuery.sanitize_session_name(raw_session_name)
        sts_config = open_api_models.Config(
            access_key_id=master_ak_id,
            access_key_secret=master_ak_secret,
            endpoint='sts.aliyuncs.com'
        )
        sts_client = StsClient(sts_config)
        assume_role_request = AssumeRoleRequest(
            role_arn=role_arn,
            role_session_name=api_safe_session_name,
            duration_seconds=3600
        )
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
        if os.path.exists(sbu_csv_path):
            with open(sbu_csv_path, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    iid = row.get('ID', '').strip()
                    if iid:
                        try:
                            # CAS 兜底强制按 12.0 (1年) 处理
                            duration_map[iid] = float(row.get('续费时长（月）', '12.0').strip())
                        except ValueError:
                            duration_map[iid] = 12.0
        return duration_map

    @staticmethod
    def extract_config_string_from_row(row: Dict[str, str]) -> str:
        for k, v in row.items():
            if isinstance(v, str) and ('Brand:' in v or 'Domain Type:' in v or '品牌:' in v):
                return v
        return row.get('实例配置', row.get('属性', ''))

    @classmethod
    def get_target_bill_month(cls, expire_str: str, duration: float) -> str:
        match = re.search(r'(\d{4})[-/](\d{1,2})', expire_str)
        if not match:
            return ""
        year = int(match.group(1))
        month = int(match.group(2))
        duration_int = int(duration)

        if duration_int == 1:
            if month == 1:
                year -= 1
                month = 12
            else:
                month -= 1
        elif duration_int == 12:
            year -= 1
        else:
            return ""

        return f"{year}-{month:02d}"

    @classmethod
    def get_historical_config(cls, instance_id: str, expire_time_str: str, duration_val: float) -> str:
        target_month_str = cls.get_target_bill_month(expire_time_str, duration_val)
        if not target_month_str:
            return ""

        if target_month_str not in cls._historical_bills_cache:
            file_path = os.path.join(DATA_DIR, f'aliyun_bill_{target_month_str}.csv')
            cls._historical_bills_cache[target_month_str] = {}

            if os.path.exists(file_path):
                logger.info(f"📂 [缓存未命中] 正在加载历史基础账单文件至内存: {file_path}")
                with open(file_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        iid = row.get('实例ID', row.get('资源id', row.get('InstanceID', ''))).strip()
                        if iid:
                            cls._historical_bills_cache[target_month_str][iid] = row
            else:
                logger.warning(f"⚠️ [历史账单缺失] 找不到预期的基础账单文件: {file_path}")

        hist_row = cls._historical_bills_cache[target_month_str].get(instance_id)
        if hist_row:
            config_str = cls.extract_config_string_from_row(hist_row)
            if config_str:
                logger.info(f"🔍 [历史账单穿透成功] 已从 {target_month_str} 账单中提取到 [{instance_id}] 的配置数据。")
                return config_str

        logger.warning(f"⚠️ [历史账单穿透失败] 在 {target_month_str} 账单中未找到 ID [{instance_id}] 或未找到配置列。")
        return ""

    @staticmethod
    def parse_cas_attributes_to_modulelist(config_str: str) -> Tuple[List[Any], Dict[str, str]]:
        raw_str_lower = config_str.lower()

        # 1. 映射品牌
        brand = 'digicert'
        if 'alibaba' in raw_str_lower:
            brand = 'alibaba'
        elif 'globalsign' in raw_str_lower:
            brand = 'globalsign'
        elif 'geotrust' in raw_str_lower:
            brand = 'geotrust'
        elif 'rapid' in raw_str_lower:
            brand = 'rapid'

        # 2. 映射域名类型
        domain_type = 'one'
        if 'wildcard' in raw_str_lower or '通配' in raw_str_lower:
            domain_type = 'all'
        elif 'multiple' in raw_str_lower or '多' in raw_str_lower:
            domain_type = 'multiple'

        # 3. 映射证书等级 (cer_type) - 核心修复区
        cer_type = 'starter'
        if 'ov ' in raw_str_lower or 'ovssl' in raw_str_lower or 'personal' in raw_str_lower:
            cer_type = 'personal'
        elif 'ev ' in raw_str_lower or 'evssl' in raw_str_lower or 'advanced' in raw_str_lower:
            cer_type = 'advanced'
        else:
            # ⭐ 只要是通配符、多域名，或者第三方品牌的 DV，在底层 SKU 中必须属于付费标准版(base)，绝不可能是入门版(starter)
            if domain_type == 'all' or domain_type == 'multiple' or brand != 'alibaba':
                cer_type = 'base'
            else:
                cer_type = 'starter'

        # 4. 数量统计
        wildcard_count = 1 if domain_type == 'all' else 0
        full_count = 1 if domain_type == 'one' else 0
        domain_num = max(1, wildcard_count + full_count)

        # 5. 生成 Spec 缩写
        prefix_map = {'alibaba': 'ali', 'digicert': 'ss', 'globalsign': 'gs', 'geotrust': 'geo', 'rapid': 'rap'}
        level_map = {'starter': 'dv', 'base': 'dv', 'personal': 'ov', 'advanced': 'ev'}

        b_prefix = prefix_map.get(brand, 'ss')
        c_level = level_map.get(cer_type, 'dv')

        wildcard_spec = f"{b_prefix}.{c_level}.w"
        full_spec = f"{b_prefix}.{c_level}.f"

        # 6. 组装 BSS 强制 ModuleList
        modules = []
        modules.append(bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
            module_code='domain_num',
            config=f"domain_type:{domain_type},cer_type:{cer_type},domain_num:{domain_num},brand:{brand}"
        ))
        modules.append(bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
            module_code='cer_type',
            config=f"domain_type:{domain_type},cer_type:{cer_type},brand:{brand}"
        ))

        if wildcard_count > 0:
            modules.append(bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='wildcardDomainCount',
                config=f"wildcardDomainCount:{wildcard_count},wildcardSpec:{wildcard_spec}"
            ))
        if full_count > 0:
            modules.append(bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='fullDomainCount',
                config=f"fullDomainCount:{full_count},fullSpec:{full_spec}"
            ))

        extracted_info = {
            "解析出的品牌": brand, "解析出的类型": domain_type,
            "底层映射等级(重要)": cer_type, "总域名数": domain_num,
            "组装通配符Spec": wildcard_spec if wildcard_count else "无"
        }

        return modules, extracted_info

    @classmethod
    def query_cas_price_via_bss(
            cls,
            bss_client: BssOpenApi20171214Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int,
            duration_val: float
    ) -> Tuple[float, float, str, Dict]:

        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', '').strip()
        product_type = item.get('产品类型', '').strip()
        expire_time = item.get('资源到期时间', '').strip()

        config_str = cls.get_historical_config(instance_id, expire_time, duration_val)

        if not config_str:
            logger.info(f"🔄 正在降级读取当前实例所在行的配置数据...")
            config_str = cls.extract_config_string_from_row(item)

        if not config_str:
            logger.warning(f"⚠️ 无法在任何来源中提取到 {instance_id} 的证书配置(Brand/Type)，可能导致 BSS 报错。")
            module_list, info = [], {}
        else:
            module_list, info = cls.parse_cas_attributes_to_modulelist(config_str)
            VisualLogger.print_panel(f"CSV 逆向解析配置指纹 - [{instance_id}]", info)

        try:
            request = bss_open_api_20171214_models.GetSubscriptionPriceRequest(
                subscription_type='Subscription',
                service_period_unit=period_unit,
                product_code='cas',
                product_type=product_type if product_type else None,
                order_type='Renewal',
                instance_id=instance_id,
                region=region_id,
                service_period_quantity=period_quantity,
                module_list=module_list
            )

            VisualLogger.print_panel(f"BSS GetSubscriptionPrice Payload - [{instance_id}]", request.to_map())

            response = bss_client.get_subscription_price_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map()

            if res_body.get('Success'):
                VisualLogger.print_panel(f"BSS Raw Response (Success) - [{instance_id}]", res_body)
                data = res_body.get('Data', {})
                return float(data.get('TradePrice', 0.0)), float(data.get('OriginalPrice', 0.0)), data.get('Currency',
                                                                                                           'CNY'), info
            else:
                msg = res_body.get('Message', 'API 询价失败')
                code = res_body.get('Code', 'N/A')
                VisualLogger.print_panel(f"BSS Raw Response (Failed) - [{instance_id}]", res_body, level='error')
                return -1.0, -1.0, f"BSS拒绝: {code} / {msg}", info

        except Exception as e:
            logger.error(f"❌ [SDK 致命异常] ID: {instance_id} | \n{str(e)}")
            return -1.0, -1.0, "API_EXCEPTION: 底层抛错", info

    @staticmethod
    def main():
        logger.info("★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★ [STEP: STARTING API QUERY PROCESS] ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")
        logger.info("🚀 阿里云 数字证书管理服务 (CAS) 国际站续费询价工具 (SKU防撞墙精修版)")

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        first_day_of_next_month = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month_str = first_day_of_next_month.strftime("%Y-%m")
        current_month_str = today.strftime("%Y-%m")
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置到期数据清单 {input_csv_path}。")
            sys.exit(1)

        session_to_section = AliyunCasRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunCasRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        total_count = 0
        filtered_count = 0

        logger.info(f"📂 正在读取 本期续费清单: {input_csv_path}")
        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                total_count += 1
                instance_id = row.get('资源id', '').strip()
                product_code = row.get('产品代码', '').lower().strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_norm = row.get('资源到期时间', '').strip().replace('/', '-')

                if product_code == 'cas' or instance_id.startswith('cas-') or instance_id.startswith('cas_'):
                    if account and expire_time_norm.startswith(next_month_str):
                        instances_by_account.setdefault(account, []).append(row)
                        filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【cas + 下月到期】条件的实例。")

        output_fields = ['资源id', '资源所属账号', '资源到期时间', '产品代码', '描述', '原价', '折扣', '货币单位',
                         '最终价']
        if not os.path.exists(output_csv_path):
            with open(output_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                csv.DictWriter(f, fieldnames=output_fields).writeheader()

        total_processed, total_success, total_failed = 0, 0, 0

        for account, items in instances_by_account.items():
            logger.info(f"\n" + "═" * 80)
            logger.info(f"▶▶ 🏁 账号集群处理开始: [{account}] | 待处理 CAS 总数: {len(items)}")
            section = session_to_section.get(account)
            if not section: continue

            try:
                credentials = AliyunCasRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token,
                    endpoint='business.ap-southeast-1.aliyuncs.com'
                )
                bss_client = BssOpenApi20171214Client(open_config)
            except Exception as e:
                logger.error(f"❌ 客户端初始化失败: {str(e)}")
                continue

            processed_rows = []
            for item in items:
                total_processed += 1
                instance_id = item.get('资源id', '').strip()

                duration_val = duration_map.get(instance_id, 12.0)
                period_unit, period_quantity = ('Year', 1) if duration_val == 12.0 else ('Month', (
                    int(duration_val) if duration_val > 0 else 1))

                logger.info(f"⏳ 正在处理: {instance_id} | 续费周期判定: {period_quantity} {period_unit}")

                trade_price, original_price, status_currency, parsed_info = AliyunCasRenewalPriceQuery.query_cas_price_via_bss(
                    bss_client, item, period_unit, period_quantity, duration_val
                )

                out_row = {
                    '资源id': instance_id, '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''), '产品代码': item.get('产品代码', 'cas'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    total_success += 1
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_currency
                    out_row['折扣'] = round(original_price - trade_price, 2) if original_price > trade_price else 0.0

                    desc = f"解析成功-> 品牌:{parsed_info.get('解析出的品牌')} 等级:{parsed_info.get('底层映射等级(重要)')} "
                    out_row['描述'] = desc
                    logger.info(f"✅ 询价成功! ID: {instance_id} | 最终价: {trade_price} {status_currency}")
                else:
                    total_failed += 1
                    out_row['描述'] = f"API受阻: {status_currency}"
                    logger.warning(f"❌ 询价受阻记录已生成: ID: {instance_id} | 状态: {status_currency}")

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                csv.DictWriter(f, fieldnames=output_fields).writerows(processed_rows)

        logger.info("\n★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★ [STEP: EXECUTION COMPLETE] ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")
        VisualLogger.print_panel("Final Details", {
            "Total Processed": total_processed, "Success": total_success, "Failed": total_failed
        })


if __name__ == '__main__':
    AliyunCasRenewalPriceQuery.main()
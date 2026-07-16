# -*- coding: utf-8 -*-
"""
阿里云 WAF (Web Application Firewall) 真实续费开销询价工具 (国际/国内通用版)
功能：
1. 账单路由：根据 csv 的 ProductType 自动分发 WAF 2.0 / 3.0 原生探针。
2. 深度翻译：【WAF 3.0 专属】将探针查出的复杂 Details 字段，完美翻译为 BSS 计费引擎所需的全套积木参数。
3. 【大小写极客修复】：严格对齐 BSS 字典，将 PackageCode 转换为首字母大写 (Enterprise, Pro)。
4. 【核心修正】：BSS 财务中心 Endpoint 已替换为国际站专属节点 (ap-southeast-1)。
5. 【极具可视化的探针全显】：强制以高可读性格式在控制台全量输出属性字典与询价 Payload。
6. 【时间筛选增强】：自动计算并严格过滤出“下月到期”的资源进行精准询价。
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

# WAF 3.0 SDK
try:
    from alibabacloud_waf_openapi20211001.client import Client as Waf3Client
    from alibabacloud_waf_openapi20211001 import models as waf3_models
except ImportError:
    print("❌ 缺少 WAF 3.0 SDK，请运行: pip install alibabacloud_waf-openapi20211001")
    sys.exit(1)

# WAF 2.0 SDK
try:
    from alibabacloud_waf_openapi20190910.client import Client as Waf2Client
    from alibabacloud_waf_openapi20190910 import models as waf2_models
except ImportError:
    print("❌ 缺少 WAF 2.0 SDK，请运行: pip install alibabacloud_waf-openapi20190910")
    sys.exit(1)

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/waf_renewal_price.log'))


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

        self.logger = logging.getLogger('waf_renewal_visual')
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
class AliyunWafRenewalPriceQuery:
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
        master_ak_id, master_ak_secret = AliyunWafRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')
        api_safe_session_name = AliyunWafRenewalPriceQuery.sanitize_session_name(raw_session_name)
        sts_config = open_api_models.Config(access_key_id=master_ak_id, access_key_secret=master_ak_secret,
                                            endpoint='sts.aliyuncs.com')
        sts_client = StsClient(sts_config)
        return sts_client.assume_role(AssumeRoleRequest(role_arn=role_arn, role_session_name=api_safe_session_name,
                                                        duration_seconds=3600)).body.credentials

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

    # ================== 核心：WAF 底层探针与 BSS 字典映射 ==================
    @staticmethod
    def fetch_waf_physical_specs(open_config: open_api_models.Config, region_id: str, instance_id: str,
                                 product_type: str) -> dict:

        # [核心增强] WAF 探针全球路由逻辑：只要不是国内 cn- 打头的节点，都去国际网关查
        # 因为 WAF 是全局防护资源，它的管控 API 通常只收敛在杭州(中国站)和新加坡(国际站)
        waf_api_region = 'cn-hangzhou' if str(region_id).startswith('cn-') else 'ap-southeast-1'
        open_config.endpoint = f'wafopenapi.{waf_api_region}.aliyuncs.com'

        if not product_type:
            raise ValueError("CSV 数据中缺少 ProductType 字段，无法判断调用 WAF 2.0 还是 3.0 的探针。")

        specs = {
            'Region': waf_api_region,
            'ProductType': product_type,
            'IsWaf3': False
        }

        if product_type in ['waf_v3prepaid_public_cn', 'waf_v3', 'waf_v3prepaid_public_intl']:
            specs['IsWaf3'] = True
            logger.info(f"🔍 [WAF Probe] ➡️ 调度 WAF 3.0 API 探测底层规格: {instance_id}")
            try:
                waf3_client = Waf3Client(open_config)
                req3 = waf3_models.DescribeInstanceRequest()
                req3.instance_id = instance_id
                req3.region_id = waf_api_region

                resp3 = waf3_client.describe_instance_with_options(req3, util_models.RuntimeOptions())
                body3 = resp3.body.to_map() if hasattr(resp3.body, 'to_map') else dict(resp3.body)

                logger.print_block(f"WAF 3.0 DescribeInstance Result - [{instance_id}]", body3)

                if not body3:
                    raise ValueError("WAF 3.0 DescribeInstance API 返回体为空。")

                edition = body3.get('Edition', '')
                if not edition:
                    raise ValueError("WAF 3.0 API 返回的数据中不包含 Edition (PackageCode) 信息。")

                # BSS 字典中的首字母大写格式 (Basic, Pro, Enterprise)
                specs['PackageCode'] = edition.capitalize()

                # WAF 3.0 深度字典翻译
                details = body3.get('Details', {})
                if details:
                    specs['QPSPackage'] = str(details.get('ExtendQps', 0))
                    specs['ExtDomainPackage'] = str(details.get('ExtDomainPackage', 0))
                    if details.get('LogService'):
                        specs['LogStorage'] = '3'
                    specs['botWeb'] = 'True' if details.get('BotWeb') else 'False'
                    specs['botApp'] = 'True' if details.get('BotApp') else 'False'
                    specs['apisec'] = 'True' if details.get('ApiSec') else 'False'
                    specs['WafGslb'] = 'True' if details.get('Gslb') else 'False'
                    specs['bot_version'] = '3'

                logger.info(f"👉 [WAF Probe] WAF 3.0 翻译完毕 -> BSS计费指纹: {specs}")
                return specs
            except Exception as e:
                raise ValueError(f"WAF 3.0 探针执行失败: {str(e)}")

        elif product_type in ['waf', 'waf-cas']:
            logger.info(f"🔍 [WAF Probe] ➡️ 调度 WAF 2.0 API 探测底层规格: {instance_id}")
            try:
                waf2_client = Waf2Client(open_config)
                req2 = waf2_models.DescribeInstanceInfoRequest()
                req2.instance_id = instance_id

                resp2 = waf2_client.describe_instance_info_with_options(req2, util_models.RuntimeOptions())
                body2 = resp2.body.to_map() if hasattr(resp2.body, 'to_map') else dict(resp2.body)

                logger.print_block(f"WAF 2.0 DescribeInstanceInfo Result - [{instance_id}]", body2)

                if not body2 or not body2.get('InstanceInfo'):
                    raise ValueError("WAF 2.0 DescribeInstanceInfo API 返回体为空。")

                info = body2.get('InstanceInfo', {})
                version_code = info.get('Version', '')
                if not version_code:
                    raise ValueError("WAF 2.0 API 返回的数据中不包含 Version (PackageCode) 信息。")

                # WAF 2.0 的 version 通常是小写
                specs['PackageCode'] = version_code.lower()

                logger.info(f"👉 [WAF Probe] WAF 2.0 提取成功 -> BSS计费指纹: {specs}")
                return specs
            except Exception as e:
                raise ValueError(f"WAF 2.0 探针执行失败: {str(e)}")
        else:
            raise ValueError(f"未知的 ProductType: [{product_type}]，系统无法路由对应的 WAF 探针。")

    # ================== 精准 BSS 询价逻辑 ==================
    @staticmethod
    def query_waf_price_via_bss(
            bss_client: BssOpenApi20171214Client,
            open_config: open_api_models.Config,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, str]:

        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', item.get('资源所在地域', 'cn-hangzhou')).strip()
        product_code = item.get('产品代码', 'waf').lower().strip()

        # 补全容错逻辑，防止旧版账单未带 ProductType
        product_type = item.get('产品类型', item.get('ProductType', '')).lower().strip()
        if not product_type:
            if product_code == 'waf_v3':
                product_type = 'waf_v3prepaid_public_cn'
            elif product_code == 'waf':
                product_type = 'waf'

        try:
            specs = AliyunWafRenewalPriceQuery.fetch_waf_physical_specs(open_config, region_id, instance_id,
                                                                        product_type)

            target_region = specs['Region']
            target_pkg_code = specs['PackageCode']
            target_prod_type = specs['ProductType']

            # 初始化基础模块
            module_list_params = [
                {"Config": f"Region:{target_region}", "ModuleCode": "Region"},
                {"Config": f"PackageCode:{target_pkg_code}", "ModuleCode": "PackageCode"}
            ]

            spec_desc = f"架构: {target_prod_type} | 版本: {target_pkg_code}"

            # WAF 3.0 附加组件模块装配
            if specs.get('IsWaf3'):
                for bss_code in ['QPSPackage', 'ExtDomainPackage', 'LogStorage', 'botWeb', 'botApp', 'apisec',
                                 'WafGslb', 'bot_version']:
                    if bss_code in specs:
                        module_list_params.append({"Config": f"{bss_code}:{specs[bss_code]}", "ModuleCode": bss_code})
                        spec_desc += f" | {bss_code}: {specs[bss_code]}"

            module_list_objects = [
                bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                    module_code=m['ModuleCode'], config=m['Config']
                ) for m in module_list_params
            ]

            request_log_payload = {
                "subscription_type": 'Subscription',
                "service_period_unit": period_unit,
                "product_code": product_code,
                "product_type": target_prod_type,
                "order_type": 'Renewal',
                "instance_id": instance_id,
                "region": target_region,
                "service_period_quantity": period_quantity,
                "module_list": module_list_params
            }
            logger.print_block(f"BSS GetSubscriptionPrice Payload - [{instance_id}]", request_log_payload)

            request = bss_open_api_20171214_models.GetSubscriptionPriceRequest(
                subscription_type='Subscription',
                service_period_unit=period_unit,
                product_code=product_code,
                product_type=target_prod_type,
                order_type='Renewal',
                instance_id=instance_id,
                module_list=module_list_objects,
                region=target_region,
                service_period_quantity=period_quantity
            )

            response = bss_client.get_subscription_price_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            logger.print_block(f"BSS Raw Response - [{instance_id}]", res_body)

            if res_body.get('Success'):
                data = res_body.get('Data', {})
                trade_price = float(data.get('TradePrice', 0.0))
                original_price = float(data.get('OriginalPrice', 0.0))
                currency = data.get('Currency', 'CNY')  # 核心：国际站将自动返回 USD

                if original_price > trade_price:
                    spec_desc += " | 🎁已含系统折扣"

                return trade_price, original_price, currency, spec_desc
            else:
                msg = res_body.get('Message', 'API 询价失败')
                code = res_body.get('Code', '')
                logger.error(f"❌ [API 返回异常] 状态码: {code} | 详情: {msg}")
                return -1.0, -1.0, f"BSS拒绝: [{code}] {msg}", spec_desc

        except Exception as e:
            logger.error(f"❌ [询价异常] 探针执行失败: {str(e)}")
            return -1.0, -1.0, f"探针崩溃: {str(e)}", ""

    @staticmethod
    def main():
        logger.print_step("PROGRAM START: WAF RENEWAL PRICE QUERY (GLOBAL EDITION)")

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

        session_to_section = AliyunWafRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunWafRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                total_count += 1
                iid = row.get('资源id', '').strip()
                pcode = row.get('产品代码', '').lower().strip()
                acc = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()

                expire_time_norm = expire_time_raw.replace('/', '-')

                if pcode in ['waf', 'waf-cas', 'waf_v3'] or iid.startswith('waf-') or iid.startswith('waf_'):
                    if acc:
                        if expire_time_norm.startswith(next_month_str):
                            instances_by_account.setdefault(acc, []).append(row)
                            filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【WAF(Web应用防火墙) + 下月到期】条件的实例。")

        output_fields = ['资源id', '资源所属账号', '资源到期时间', '产品代码', '描述', '原价', '折扣', '货币单位',
                         '最终价']
        if not os.path.exists(output_csv_path):
            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                csv.DictWriter(f, fieldnames=output_fields).writeheader()

        total_processed, total_success, total_failed = 0, 0, 0

        logger.print_step("STARTING API QUERY PROCESS")
        for account, items in instances_by_account.items():
            logger.info(f"\n▶▶ 🏁 账号集群: [{account}] | 待处理 WAF 实例总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunWafRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token
                )

                # ========================== 核心修复 ==========================
                # BSS 财务型接口：针对国际站(alibabacloud) 必须指定 ap-southeast-1 节点
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
                d_val = duration_map.get(instance_id, 1.0)
                p_unit, p_qty = ('Year', 1) if d_val == 12.0 else ('Month', int(d_val) if d_val > 0 else 1)

                trade, original, curr, desc = AliyunWafRenewalPriceQuery.query_waf_price_via_bss(
                    bss_client, open_config, item, p_unit, p_qty
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': item.get('产品代码', 'waf'),
                    '描述': '',
                    '原价': '',
                    '折扣': '',
                    '货币单位': '',
                    '最终价': ''
                }

                if trade >= 0:
                    total_success += 1
                    out_row.update({
                        '最终价': trade,
                        '原价': original,
                        '货币单位': curr,
                        '折扣': round(original - trade, 2) if original > trade else 0.0,
                        '描述': desc
                    })
                    logger.info(f"✅ 询价成功! ID: {instance_id} | 最终价: {trade} {curr}")
                else:
                    total_failed += 1
                    out_row['描述'] = f"API受阻: {curr}"
                    logger.error(f"❌ API拒绝: ID: {instance_id} | {curr}")

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                csv.DictWriter(f, fieldnames=output_fields).writerows(processed_rows)

        logger.print_step("EXECUTION COMPLETE")
        logger.print_block("Final Details", {
            "Total Processed": total_processed,
            "Success": total_success,
            "Failed": total_failed,
            "Output Path": output_csv_path,
            "Write Mode": "Append ('a')"
        })


if __name__ == '__main__':
    AliyunWafRenewalPriceQuery.main()
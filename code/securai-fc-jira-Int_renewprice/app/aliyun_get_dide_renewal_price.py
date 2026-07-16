# -*- coding: utf-8 -*-
"""
阿里云 DIDE (DataWorks/数据工场) 专属续费询价工具 (账单溯源版)
功能：
1. 【账单溯源】：放弃 API 探针，通过实例 ID 自动反查历史真实消费账单（上月或去年同月）获取最终价。
2. 【动态过滤】：筛选产品代码包含 'dide' 的云资源，精准定位 DataWorks 相关产品。
3. 【表头自适应】：兼容历史账单中的 `消费金额`、`原价` 等多种表头字段变体。
4. 【时间修正】：短周期续费强制锁定“系统时间上个月”的已出账单，避免当月账单未生成的报错。
"""

import os
import sys
import csv
import logging
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
# 日志文件已修改为 dide 专属
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/dide_renewal_price.log'))


def init_logger() -> logging.Logger:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_format = logging.Formatter('%(asctime)s [%(levelname)s] [Line %(lineno)d] - %(message)s')
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


class AliyunDideRenewalPriceQuery:

    @staticmethod
    def load_duration_mapping() -> Dict[str, float]:
        """从 Jira 工单中加载每个资源的所需续费时长（月）"""
        sbu_csv_path = os.path.join(DATA_DIR, 'jira_get_renewal_sbu_issues.csv')
        duration_map = {}
        if not os.path.exists(sbu_csv_path):
            logger.warning(f"⚠️ 未找到 Jira 周期工单文件 {sbu_csv_path}，默认按 1.0 个月处理。")
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
            logger.error(f"❌ 读取续费时长失败: {str(e)}")
        return duration_map

    @staticmethod
    def calculate_target_bill_month(expire_time_str: str, duration_months: float) -> Optional[str]:
        """根据资源的到期时间和续费周期，推算出应该去查哪个月的历史账单"""
        if not expire_time_str or expire_time_str == '未知':
            return None

        try:
            clean_time_str = expire_time_str.replace('T', ' ').replace('Z', '').strip()
            expire_dt = datetime.strptime(clean_time_str, "%Y-%m-%d %H:%M:%S")
            today = datetime.today()

            if duration_months == 12.0:
                # 12个月的周期：通常取去年同期的账单
                target_year = expire_dt.year - 1
                target_month = expire_dt.month
            else:
                # 1个月的周期：强制取系统当前时间的“上个月”账单
                first_day_of_current = today.replace(day=1)
                last_month_dt = first_day_of_current - timedelta(days=1)
                target_year = last_month_dt.year
                target_month = last_month_dt.month

            return f"{target_year}-{target_month:02d}"
        except Exception as e:
            logger.error(f"   [时间解析] 无法解析到期时间 {expire_time_str}: {str(e)}")
            return None

    @staticmethod
    def query_price_from_history_bill(instance_id: str, target_month_str: str) -> Tuple[float, float, str, str]:
        """打开对应的历史账单寻找实例的购买金额"""
        bill_path = os.path.join(DATA_DIR, f'aliyun_bill_{target_month_str}.csv')

        if not os.path.exists(bill_path):
            logger.warning(f"   [账单溯源] 历史账单文件不存在: {bill_path}")
            return -1.0, -1.0, "CNY", f"缺少历史账单 ({target_month_str})"

        logger.debug(f"   [账单溯源] 正在扫描历史账单: {bill_path}")

        try:
            with open(bill_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('实例ID', '').strip() == instance_id or row.get('资源id', '').strip() == instance_id:
                        try:
                            pretax_amount = row.get('原价', row.get('PretaxAmount', row.get('PretaxGrossAmount',
                                                                                            row.get('消费金额', 0))))
                            payment_amount = row.get('最终价', row.get('PaymentAmount', row.get('消费金额', 0)))
                            currency = row.get('币种', row.get('Currency', 'CNY'))

                            original_price = float(pretax_amount) if pretax_amount else 0.0
                            trade_price = float(payment_amount) if payment_amount else 0.0

                            if trade_price > 0 or original_price > 0:
                                return trade_price, original_price, currency, f"依据 {target_month_str} 账单溯源"
                        except ValueError:
                            continue

            return -1.0, -1.0, "CNY", f"在 {target_month_str} 账单中未找到支付记录"
        except Exception as e:
            logger.error(f"   [账单溯源] 读取账单 {bill_path} 异常: {str(e)}")
            return -1.0, -1.0, "CNY", f"读取账单失败: {str(e)}"

    @staticmethod
    def main():
        logger.info("=" * 100)
        logger.info("  🚀 阿里云 DIDE (DataWorks) 续费估价工具 (账单溯源版)")
        logger.info("=" * 100)

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        current_month_str = today.strftime("%Y-%m")
        # 结果输出文件命名为 dide
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置待续费数据清单 {input_csv_path}。")
            sys.exit(1)

        duration_map = AliyunDideRenewalPriceQuery.load_duration_mapping()

        # 按账号归类 DIDE 资源
        instances_by_account = {}
        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                pcode = row.get('产品代码', '').lower().strip()
                acc = row.get('资源所属账号', '').strip()

                # 专门过滤 DIDE 产品
                if 'dide' in pcode:
                    if acc:
                        instances_by_account.setdefault(acc, []).append(row)

        output_fields = ['资源id', '资源所属账号', '资源到期时间', '产品代码', '描述', '原价', '折扣', '货币单位',
                         '最终价']
        if not os.path.exists(output_csv_path):
            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                csv.DictWriter(f, fieldnames=output_fields).writeheader()

        if not instances_by_account:
            logger.warning(f"⚠️ 没有在清单中发现 DIDE 资源，程序正常退出。")
            return

        for account, items in instances_by_account.items():
            logger.info(f"\n🏁 账号集群: [{account}] | 符合条件的 DIDE 实例总数: {len(items)}")

            processed_rows = []
            for item in items:
                instance_id = item.get('资源id', '').strip()
                expire_time = item.get('资源到期时间', '').strip()
                duration_months = duration_map.get(instance_id, 1.0)

                logger.info(f"\n" + "🔍 " * 25)
                logger.info(f"   [DIDE Details] 正在溯源 DataWorks 明细:")
                logger.info(f"      ├─ 📌 资源 ID      : {instance_id}")
                logger.info(f"      ├─ ⏳ 目标续费时长 : {duration_months} 个月")
                logger.info(f"      └─ 📅 当前到期时间 : {expire_time}")

                target_bill_month = AliyunDideRenewalPriceQuery.calculate_target_bill_month(expire_time,
                                                                                            duration_months)

                trade, original, curr, desc = -1.0, -1.0, "CNY", "无法计算目标账单月份"

                if target_bill_month:
                    logger.info(f"      => 📡 溯源指向     : 目标历史账单 [aliyun_bill_{target_bill_month}.csv]")
                    trade, original, curr, desc = AliyunDideRenewalPriceQuery.query_price_from_history_bill(instance_id,
                                                                                                            target_bill_month)

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': expire_time,
                    '产品代码': item.get('产品代码', 'dide'),
                    '描述': '', '原价': '', '折扣': '',
                    '货币单位': '', '最终价': ''
                }

                if trade >= 0:
                    out_row.update({
                        '最终价': trade,
                        '原价': original,
                        '货币单位': curr,
                        '折扣': round(original - trade, 2) if original > trade else 0.0,
                        '描述': desc
                    })
                    logger.info(f"   └─ ✅ 溯源成功! ID: {instance_id} | 最终账单价: {trade} {curr}")
                else:
                    out_row['描述'] = f"无历史账单: {desc}"
                    logger.error(f"   └─ ❌ 溯源失败: ID: {instance_id} | {desc}")

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                csv.DictWriter(f, fieldnames=output_fields).writerows(processed_rows)

        logger.info(f"\n" + "=" * 100)
        logger.info(f"✅ 执行完毕。结果追加写入：{output_csv_path}")


if __name__ == '__main__':
    AliyunDideRenewalPriceQuery.main()
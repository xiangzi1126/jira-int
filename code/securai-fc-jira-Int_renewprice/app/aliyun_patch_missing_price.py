# -*- coding: utf-8 -*-
"""
阿里云询价遗漏资源修补工具 (aliyun_patch_missing_price.py)
功能：
1. 读取【上月】的续费账单 (aliyun_renewal_bill_YYYY-MM.csv) 和【本月】的询价结果 (aliyun_renewal_price_YYYY-MM.csv)。
2. 【严格筛选】：仅核对账单中“资源到期时间”在【下个月】的资源。
3. 【自动修补】：找出在账单中但未被询价的资源，进行双重兜底：
   - 将“描述”字段强制标记为“该资源未被查询，请联络管理员”。
   - 将“最终价”强制赋为 0。
4. 【无损追加】：严格对齐询价结果文件的原生表头，将遗漏资源直接追加写入 aliyun_renewal_price 文件中。
"""

import os
import sys
import csv
import logging
import calendar
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/patch_missing_price.log'))


# ===================== 日志初始化配置 =====================
def init_logger() -> logging.Logger:
    """初始化可视化日志配置"""
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


class AliyunMissingResourcePatcher:

    @staticmethod
    def get_next_month_str(current_date: datetime) -> str:
        """精准计算下个月的 YYYY-MM 字符串"""
        days_in_month = calendar.monthrange(current_date.year, current_date.month)[1]
        next_month_date = current_date + timedelta(days=days_in_month)
        return next_month_date.strftime("%Y-%m")

    @staticmethod
    def get_last_month_str(current_date: datetime) -> str:
        """精准计算上个月的 YYYY-MM 字符串"""
        first_day_of_current = current_date.replace(day=1)
        last_month_date = first_day_of_current - timedelta(days=1)
        return last_month_date.strftime("%Y-%m")

    @staticmethod
    def main():
        logger.info("════════════════════════════════════════════════════════════════════════════════")
        logger.info(" 🚀 阿里云询价遗漏资源修补工具 (Patch Missing Price)")
        logger.info("════════════════════════════════════════════════════════════════════════════════")

        # 1. 计算时间节点
        today = datetime.today()
        current_month_str = today.strftime("%Y-%m")
        last_month_str = AliyunMissingResourcePatcher.get_last_month_str(today)
        next_month_str = AliyunMissingResourcePatcher.get_next_month_str(today)

        logger.info(
            f"📅 执行周期: 当前询价月份 [{current_month_str}] | 对比账单月份: [{last_month_str}] | 目标过滤到期月份: [{next_month_str}]")

        # 2. 构造文件路径
        bill_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')
        price_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        # 3. 前置文件校验
        if not os.path.exists(bill_csv_path):
            logger.error(f"❌ 致命错误: 未找到账单文件: {bill_csv_path}")
            sys.exit(1)
        if not os.path.exists(price_csv_path):
            logger.error(f"❌ 致命错误: 未找到询价结果文件: {price_csv_path}")
            sys.exit(1)

        # 4. 读取 Price 表，获取全量已询价 ID 及表头结构
        processed_ids = set()
        price_fieldnames = []
        logger.info(f"📂 正在解析现有询价池: {os.path.basename(price_csv_path)}")

        with open(price_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                price_fieldnames = list(reader.fieldnames)
            for row in reader:
                res_id = row.get('资源id', '').strip()
                if res_id:
                    processed_ids.add(res_id)

        logger.info(f"   └─ 成功提取已询价实例数: {len(processed_ids)} 个 | 识别到表头字段数: {len(price_fieldnames)}")

        # 5. 读取 Bill 表，过滤下月到期资源并核对遗漏
        missing_resources = []
        total_bill_rows = 0
        filtered_target_rows = 0

        logger.info(f"📂 正在深度比对账单数据: {os.path.basename(bill_csv_path)}")
        with open(bill_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)

            for row in reader:
                total_bill_rows += 1
                res_id = row.get('资源id', '').strip()
                expire_time = row.get('资源到期时间', '').strip()

                # 严格过滤：仅处理“资源到期时间”包含下个月份的资源
                if next_month_str in expire_time:
                    filtered_target_rows += 1

                    # 发现遗漏
                    if res_id and res_id not in processed_ids:
                        missing_resources.append(row)

        logger.info(
            f"   └─ 账单总扫描: {total_bill_rows} 行 | 命中 [{next_month_str}] 到期资源: {filtered_target_rows} 个")
        logger.info(f"   └─ 检出真实遗漏未询价资源: {len(missing_resources)} 个")

        # 6. 修补与追加写入
        if missing_resources:
            logger.info("════════════════════════════════════════════════════════════════════════════════")
            logger.info(" ❖ 遗漏资源修补进程启动 ❖ ")
            logger.info("════════════════════════════════════════════════════════════════════════════════")

            # 记录用于展示的日志结构
            debug_preview = []

            # 以追加模式打开 Price CSV
            with open(price_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                # extrasaction='ignore' 确保如果 bill 里有多余字段，写入时会自动丢弃，严格防撞墙
                writer = csv.DictWriter(f, fieldnames=price_fieldnames, extrasaction='ignore')

                for res in missing_resources:
                    # 强行注入统一警告描述与兜底价格
                    res['描述'] = "该资源未被查询，请联络管理员"
                    res['最终价'] = 0  # <--- 强制对齐：填入兜底价 0

                    writer.writerow(res)

                    debug_preview.append({
                        "资源ID": res.get('资源id'),
                        "产品类型": res.get('产品类型', ''),
                        "到期时间": res.get('资源到期时间', ''),
                        "最终价": res.get('最终价'),
                        "写入描述": res.get('描述')
                    })

            # 可视化打印追加的资源指纹
            logger.info("🔍 已将以下资源追加至询价结果文件:\n" + json.dumps(debug_preview, indent=4, ensure_ascii=False))
            logger.info(
                f"✅ 修补成功！{len(missing_resources)} 个遗漏资源已无缝追加至: {os.path.basename(price_csv_path)}")
        else:
            logger.info("🎉 完美！所有下月到期的资源均已存在于询价结果中，没有任何遗漏！")

        logger.info("★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★ [STEP: EXECUTION COMPLETE] ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")


if __name__ == '__main__':
    AliyunMissingResourcePatcher.main()
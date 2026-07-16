# -*- coding: utf-8 -*-
"""
阿里云询价遗漏资源核对工具
功能：
对比上月的续费账单 (aliyun_renewal_bill_YYYY-MM.csv)
和本月的询价结果 (aliyun_renewal_price_YYYY-MM.csv)，
找出存在于账单中，但未被成功询价的资源，并按账单原格式输出。
"""

import os
import sys
import csv
import logging
from datetime import datetime, timedelta

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 保持与您其他脚本一致的数据目录路径
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/check_missing.log'))


def init_logger() -> logging.Logger:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_format = logging.Formatter('%(asctime)s [%(levelname)s] - %(message)s')

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


def main():
    logger.info("=" * 80)
    logger.info("  🚀 阿里云询价遗漏资源核对工具启动")
    logger.info("=" * 80)

    # 1. 自动计算年月字符串
    today = datetime.today()
    first_day_of_current = today.replace(day=1)

    last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
    current_month_str = today.strftime("%Y-%m")

    # 2. 构造文件路径
    bill_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')
    price_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')
    output_csv_path = os.path.join(DATA_DIR, f'aliyun_missing_resources_{current_month_str}.csv')

    # 3. 检查前置文件是否存在
    if not os.path.exists(bill_csv_path):
        logger.error(f"❌ 未找到上月账单文件: {bill_csv_path}")
        sys.exit(1)
    if not os.path.exists(price_csv_path):
        logger.error(f"❌ 未找到本月询价结果文件: {price_csv_path}")
        sys.exit(1)

    # 4. 读取 Price 表，收集所有已处理的资源 ID
    processed_ids = set()
    logger.info(f"📂 正在读取询价结果文件: {os.path.basename(price_csv_path)}")
    with open(price_csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            res_id = row.get('资源id', '').strip()
            if res_id:
                processed_ids.add(res_id)

    logger.info(f"   └─ 共提取到 {len(processed_ids)} 个已询价的唯一资源 ID。")

    # 5. 读取 Bill 表，比对遗漏项
    missing_rows = []
    fieldnames = []
    logger.info(f"📂 正在比对上月账单文件: {os.path.basename(bill_csv_path)}")
    with open(bill_csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames  # 保留 Bill 表的原表头
        total_bill_count = 0

        for row in reader:
            total_bill_count += 1
            res_id = row.get('资源id', '').strip()

            # 如果资源id不在 price 表中，判定为遗漏
            if res_id and res_id not in processed_ids:
                missing_rows.append(row)

    logger.info(f"   └─ 账单总行数: {total_bill_count} | 发现遗漏资源: {len(missing_rows)} 个。")

    # 6. 结果输出
    if missing_rows:
        logger.info(f"💾 正在将遗漏明细导出至: {os.path.basename(output_csv_path)}")
        with open(output_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(missing_rows)
        logger.info(f"✅ 导出成功！请查看文件: {output_csv_path}")
    else:
        logger.info("🎉 太棒了！账单中的所有资源均已成功完成询价，没有任何遗漏。")
        # 如果没有遗漏，可选择删除旧的遗漏文件（避免误导）
        if os.path.exists(output_csv_path):
            os.remove(output_csv_path)

    logger.info("=" * 80)


if __name__ == '__main__':
    main()
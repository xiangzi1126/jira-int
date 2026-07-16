import csv
import datetime
import os


def main():
    # ---------------------- 1. 计算日期 (年-上月) ----------------------
    today = datetime.date.today()
    if today.month == 1:
        # 1月时，上月为去年12月
        target_year = today.year - 1
        target_month = 12
    else:
        target_year = today.year
        target_month = today.month - 1
    period_str = f"{target_year}-{target_month:02d}"  # 格式化为 YYYY-MM

    # ---------------------- 2. 读取源文件获取汇率 ----------------------
    source_path = '../../jira/data/resale_business_details.csv'
    rate_value = None

    try:
        with open(source_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # 获取第一行数据
            first_row = next(reader, None)
            if not first_row:
                raise ValueError("源文件为空或没有数据行")

            if '汇率' not in first_row:
                raise ValueError("源文件中未找到 '汇率' 列")

            rate_value = first_row['汇率']

    except FileNotFoundError:
        print(f"错误：找不到文件 {source_path}")
        return
    except Exception as e:
        print(f"读取源文件失败: {e}")
        return

    # ---------------------- 3. 追加写入目标文件 ----------------------
    target_path = '../../jira/data/rate.csv'

    try:
        # 检查文件是否存在，决定是否写入表头
        file_is_new = not os.path.exists(target_path)

        with open(target_path, mode='a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)

            # 如果是新文件，先写入表头
            if file_is_new:
                writer.writerow(['年月', '汇率'])

            # 追加数据
            writer.writerow([period_str, rate_value])

        print(f"成功！已记录: 年月={period_str}, 汇率={rate_value}")

    except Exception as e:
        print(f"写入目标文件失败: {e}")


if __name__ == "__main__":
    main()
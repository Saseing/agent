import json
import time
import matplotlib
matplotlib.use("Agg")  # 非交互式后端，适合服务器/脚本环境
import matplotlib.pyplot as plt
from pathlib import Path
from langchain_core.tools import tool

# 图表统一保存目录
CHART_OUTPUT_DIR = Path(__file__).parent / "pang"
CHART_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@tool
def generate_chart(
    chart_type: str,
    data: str,
    title: str = "图表",
    xlabel: str = "",
    ylabel: str = "",
    output_path: str = "",
) -> str:
    """根据数据生成可视化图表并保存为 PNG 图片。

    参数说明：
    - chart_type: 图表类型，可选值为 'line'(折线图)、'bar'(柱状图)、'pie'(饼图)、'scatter'(散点图)、'area'(面积图)
    - data: JSON 格式的图表数据，格式为 {"labels": ["标签1","标签2",...], "values": [数值1,数值2,...],
             "series": [[系列1数值...],[系列2数值...]]}。
             单系列时使用 values 字段；多系列（如多条折线）时使用 series 字段，此时 labels 为 X 轴刻度。
             饼图只需 labels + values。
    - title: 图表标题，默认为"图表"
    - xlabel: X 轴标签（饼图无效）
    - ylabel: Y 轴标签（饼图无效）
    - output_path: 图片文件名（如 "sales.png"），不传则自动生成 chart_<时间戳>.png。
             所有图片统一保存至脚本同级的 pang 目录下。
    画柱状图时，X轴学号标签只取后3位，旋转角度设为45度，并启用 tight_layout。如果数据超过20条，采用间隔5个显示刻度的方式。画折线图时，启用 grid 网格。
    返回：图表保存路径或错误信息。
    """
    try:
        # 解析数据
        chart_data = json.loads(data)
        labels = chart_data.get("labels", [])
        values = chart_data.get("values", [])
        series = chart_data.get("series", [])

        if not labels and not values and not series:
            return "错误：数据为空，请提供有效的 labels 和 values/series 数据。"

        # 设置中文字体（Windows 用 SimHei，macOS 用 Arial Unicode MS）
        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
        plt.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(figsize=(10, 6))

        if chart_type == "pie":
            # 饼图
            if not labels or not values:
                plt.close(fig)
                return "错误：饼图需要提供 labels 和 values 数据。"
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.set_title(title, fontsize=14)

        elif chart_type == "bar":
            if series:
                # 多系列柱状图
                n_series = len(series)
                bar_width = 0.8 / n_series
                x = range(len(labels))
                for i, s in enumerate(series):
                    offset = i * bar_width - 0.4 + bar_width / 2
                    ax.bar([xi + offset for xi in x], s, bar_width, label=f"系列{i+1}")
                ax.set_xticks(list(x))
                ax.set_xticklabels(labels)
                ax.legend()
            else:
                ax.bar(labels, values, color="skyblue", edgecolor="black")
            ax.set_title(title, fontsize=14)
            ax.set_xlabel(xlabel, fontsize=11)
            ax.set_ylabel(ylabel, fontsize=11)

        elif chart_type == "line":
            if series:
                for i, s in enumerate(series):
                    ax.plot(labels, s, marker="o", label=f"系列{i+1}")
                ax.legend()
            else:
                ax.plot(labels, values, marker="o", color="b", linewidth=1.5)
            ax.set_title(title, fontsize=14)
            ax.set_xlabel(xlabel, fontsize=11)
            ax.set_ylabel(ylabel, fontsize=11)
            ax.grid(True, linestyle="--", alpha=0.6)

        elif chart_type == "scatter":
            if series and len(series) >= 2:
                ax.scatter(series[0], series[1], alpha=0.7, edgecolors="black")
            else:
                x_vals = list(range(len(values)))
                ax.scatter(x_vals, values, alpha=0.7, edgecolors="black")
            ax.set_title(title, fontsize=14)
            ax.set_xlabel(xlabel, fontsize=11)
            ax.set_ylabel(ylabel, fontsize=11)
            ax.grid(True, linestyle="--", alpha=0.6)

        elif chart_type == "area":
            if series:
                for i, s in enumerate(series):
                    ax.fill_between(range(len(labels)), s, alpha=0.3, label=f"系列{i+1}")
                    ax.plot(range(len(labels)), s, marker="o", markersize=3)
                ax.set_xticks(range(len(labels)))
                ax.set_xticklabels(labels)
                ax.legend()
            else:
                ax.fill_between(range(len(labels)), values, alpha=0.3, color="skyblue")
                ax.plot(range(len(labels)), values, marker="o", color="b", markersize=4)
            ax.set_title(title, fontsize=14)
            ax.set_xlabel(xlabel, fontsize=11)
            ax.set_ylabel(ylabel, fontsize=11)
            ax.grid(True, linestyle="--", alpha=0.6)

        else:
            plt.close(fig)
            return f"错误：不支持的图表类型 '{chart_type}'，可选：line / bar / pie / scatter / area"

        plt.tight_layout()
        # 统一保存到 pang 目录
        if not output_path or not output_path.strip():
            filename = f"chart_{int(time.time())}.png"
        else:
            filename = Path(output_path).name  # 只取文件名，忽略目录部分
        out = CHART_OUTPUT_DIR / filename
        fig.savefig(str(out), dpi=150, bbox_inches="tight")
        plt.close(fig)
        return f"图表已生成并保存至：{out.resolve()}"

    except json.JSONDecodeError:
        return "错误：data 参数不是合法的 JSON 格式，请检查。"
    except Exception as e:
        return f"生成图表时出错：{e}"

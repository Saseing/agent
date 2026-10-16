def bubble_sort(arr):
    n = len(arr)
    for i in range(n):
        # 最后i个元素已有序
        for j in range(0, n-i-1):
            if arr[j] > arr[j+1]:
                arr[j], arr[j+1] = arr[j+1], arr[j]
    return arr

# 示例用法
if __name__ == "__main__":
    sample_list = [64, 34, 25, 12, 22, 11, 90]
    print("原始列表:", sample_list)
    sorted_list = bubble_sort(sample_list)
    print("排序后的列表:", sorted_list)
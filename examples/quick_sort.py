def quick_sort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quick_sort(left) + middle + quick_sort(right)

# 示例用法
if __name__ == "__main__":
    sample_list = [3, 6, 8, 10, 1, 2, 1]
    sorted_list = quick_sort(sample_list)
    print("排序后的列表:", sorted_list)
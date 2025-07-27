#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 epub_processor 的 spines 方法
"""

import sys
from pathlib import Path
import shutil

# 添加项目根目录到 Python 路径
sys.path.append(str(Path(__file__).parent))

from translator.epub_processor import EpubContent


def test_spines():
    """测试 spines 方法"""
    # EPUB 文件路径
    epub_path = Path(r"C:\Users\zerof\Downloads\Nine Stories.epub")

    if not epub_path.exists():
        print(f"错误：EPUB 文件不存在: {epub_path}")
        return

    temp_dir = Path(r"C:\Users\zerof\Downloads\test")
    try:
        print(f"正在处理 EPUB 文件: {epub_path}")
        print("=" * 60)

        # 创建 EpubContent 实例
        epub_content = EpubContent(epub_path, temp_dir)

        # 获取所有 spines
        spines = epub_content.spines

        print(f"找到 {len(spines)} 个 spine 项目:")
        print("-" * 60)

        # 打印每个 spine 的详细信息
        for i, spine in enumerate(spines, 1):
            print(f"Spine {i}:")
            print(f"  ID: {spine['id']}")
            print(f"  HREF: {spine['href']}")
            print(f"  Media Type: {spine['media_type']}")
            print(f"  Base Path: {spine['base_path']}")
            print(f"  Folder Path: {spine['folder_path']}")

            # 计算完整路径
            full_path = Path(spine["base_path"]) / spine["href"]
            if not full_path.exists():
                full_path = Path(spine["folder_path"]) / spine["href"]

            print(f"  Full Path: {full_path}")
            print(f"  Exists: {full_path.exists()}")
            print()

        # 测试 search_spine_paths 方法
        print("=" * 60)
        print("使用 search_spine_paths() 方法获取 XHTML 文件:")
        print("-" * 60)

        xhtml_count = 0
        for spine_path in epub_content.search_spine_paths():
            xhtml_count += 1
            print(f"XHTML {xhtml_count}: {spine_path}")
            print(f"  存在: {spine_path.exists()}")
            print()

        print(f"总共找到 {xhtml_count} 个 XHTML 文件")

        # 显示 EPUB 基本信息
        print("=" * 60)
        print("EPUB 基本信息:")
        print("-" * 60)
        print(f"标题: {epub_content.title}")
        print(f"作者: {epub_content.authors}")
        print(f"解压目录: {epub_content.extract_dir}")
        print(f"内容文件: {epub_content._content_path}")

        if epub_content.ncx_path:
            print(f"NCX 文件: {epub_content.ncx_path}")
        else:
            print("NCX 文件: 未找到")

    except Exception as e:
        print(f"测试过程中发生错误: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # 测试完成后删除 temp_dir
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
                print(f"已删除临时目录: {temp_dir}")
            except Exception as del_e:
                print(f"删除临时目录时出错: {del_e}")

def test_add_blank_chapter():
    """测试 add_blank_chapter 方法"""
    epub_path = Path(r"C:\Users\zerof\Downloads\Nine Stories.epub")
    epub_content = EpubContent(epub_path)
    epub_content.add_blank_chapter("new_chapter.xhtml")
    epub_content.archive(epub_path.parent / "new_epub.epub")


if __name__ == "__main__":
    # test_spines()
    test_add_blank_chapter()

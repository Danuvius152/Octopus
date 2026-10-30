"""Versioned synthetic sources for paired Wiki measurements; never user notes."""

import json
import random
from pathlib import Path

SEED = 20261030
QUESTION_FILE = Path(__file__).with_name('fixtures') / 'wiki-baseline-questions.json'
CURRENT = 'Library/Research/当前结论.md'
UPDATED = '# 当前结论\n\n## 新限制\n2026-10-31 更新：标定误差超过两像素时结论不成立。\n'


def questions():
    return json.loads(QUESTION_FILE.read_text())['questions']


def build_corpus(root: Path, *, count=100):
    """Rebuild exact source bytes independently of UUIDs and machine paths."""
    sources = {
        'Library/Me/information.md': '# 个人背景\n\n我在青峦大学学习，研究方向是合成视觉推理。\n',
        'Library/Me/边界.md': '# 资料边界\n\n没有记录职业和雇主；不能从研究方向推断职业。\n',
        'Library/Research/vidu_realtime_report/技术报告.md':
            '---\naliases: [数字人研究, Vidu实时报告]\ntags: [实时成像, 视频蒸馏]\n---\n'
            '# Vidu S1 / S2 与实时视频蒸馏技术报告\n\n'
            '## 研究结论\n视频蒸馏提高吞吐量，不等于端到端延迟得到验证。条件 A 仍未实证。\n\n'
            + ''.join(f'## 实验条件 {index}\n\n' + '固定帧长的合成测试不能替代真实交互任务；需要明确测量边界。\n' * 65 + '\n' for index in range(16))
            + '## 最后一条限制\n这些结论只适用于测试环境，缺少真实任务下的对照验证。\n',
        'Library/Research/Lumen/报告.md':
            '---\naliases: [光场小结, Lumen记录]\ntags: [实时成像, 光场]\n---\n'
            '# Lumen 光场实验报告\n\n## 研究结论\n光场重建依赖多视角观测，遮挡区域不能保证恢复。\n\n'
            + ''.join(f'## 条件 {index}\n\n' + '单次合成实验不证明跨场景泛化，需要保留相机布局和曝光条件。\n' * 45 + '\n' for index in range(12))
            + '## 末尾边界\n当前实验没有验证移动光源条件。\n',
        'Library/Research/单目重建.md':
            '---\ntags: [实时成像, 单目重建]\n---\n# 单目重建方法\n\n## 方法和条件\n'
            '单目重建使用标定后的 Rig；标定精度不足会造成几何偏差。它解决几何恢复，不等同视频生成。\n',
        'Library/Research/方法比较.md':
            '# 实时成像方法比较\n\n## 条件比较\n视频蒸馏关注吞吐量，单目重建依赖标定，光场方法需要多视角观测。\n'
            '三者的测试条件不同，不能直接把吞吐量、几何误差与视觉质量视作同一指标。\n',
        'Library/People/岑照临.md': '# 岑照临的观测笔记\n\n## 取样方法\n岑照临主张分层取样，必须报告每层的样本数与遗漏比例。\n',
        'Library/People/庄以宁.md': '# 庄以宁的观察\n\n## 复核方法\n庄以宁要求双人独立标注，分歧保留到复核，不以多数意见代替证据。\n',
        'Library/Research/校准.md': '# 校准\n\n## 研究语境\n这里的校准指相机内外参数估计，误差单位是像素。\n',
        'Library/Operations/校准.md': '# 校准\n\n## 运维语境\n这里的校准指时钟偏移测量，误差单位是毫秒。\n',
        CURRENT: '# 当前结论\n\n## 原限制\n2026-10-30 初版：只在静态光源下验证，不涉及移动光源。\n',
    }
    rng = random.Random(SEED)
    topics = ('材料整理', '观测设计', '实验记录', '数据保存', '几何基础')
    for number in range(count - len(sources)):
        topic = rng.choice(topics)
        sources[f'Library/Notes/note-{number:03}.md'] = (
            f'---\ntags: [{topic}]\n---\n# {topic} {number:03}\n\n'
            f'## 条件\n样本编号 {rng.randrange(1000000)}，这是无关合成资料，不包含个人事实。\n'
            + '结论只对记录的样本有效；应保留观察条件和原始材料。\n' * 12
        )
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())


def coverage(trace, path, revision, text):
    """Only successful returned content grants code-point ranges, never requests/snippets."""
    intervals = []
    for call in trace:
        result = call.get('response', {})
        if call['operation'] == 'read':
            ranges = [(result, result)]
        elif call['operation'] == 'read-many':
            docs = {item['id']: item for item in result.get('documents', [])}
            ranges = [(docs.get(part.get('doc'), {}), part) for part in result.get('ranges', [])]
        else:
            continue
        for document, part in ranges:
            start, end = part.get('start'), part.get('end')
            if (document.get('path') == path and document.get('revision') == revision
                    and type(start) is int and type(end) is int and 0 <= start < end <= len(text)
                    and part.get('content') == text[start:end]):
                intervals.append((start, end))
    missing, position, covered = [], 0, 0
    for start, end in sorted(intervals):
        if start > position:
            missing.append([position, start])
        covered += max(0, end - max(position, start))
        position = max(position, end)
    if position < len(text):
        missing.append([position, len(text)])
    return {'covered_characters': covered, 'duplicate_characters': sum(end - start for start, end in intervals) - covered,
            'missing': missing, 'complete': not missing}

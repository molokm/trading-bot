# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""Momentum Rotation v3-mode — текущая логика деплой-бота для сравнения.

Наследует MomentumRotation, но с use_regime=False (старая логика направления:
и лонги, и шорты без режим-фильтра) и без динамического ROI.
Используется только для бэктест-сравнения старой и новой версии.
"""

from momentum_rotation import MomentumRotation


class MomentumRotationV3(MomentumRotation):
    use_regime = False
    minimal_roi = {"0": 100}

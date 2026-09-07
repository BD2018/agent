"""安全公式求值器：AST 白名单解析，仅允许算术运算与少量数学函数。

供「本地函数型」自定义工具使用。白名单之外的语法（属性访问、下标、
推导式、lambda、导入等）在解析阶段即被拒绝，杜绝代码注入。
"""
import ast
import math
import operator


class FormulaError(ValueError):
    """公式不合法或求值失败。"""


# 公式内允许调用的函数
_ALLOWED_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
}

_BIN_OPS = {
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
}
_UNARY_OPS = {ast.UAdd, ast.USub}

_OP_NAMES = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
    ast.UAdd: "+", ast.USub: "-",
}


def _check(node: ast.AST) -> None:
    """递归校验 AST 只包含白名单节点，否则抛 FormulaError。"""
    if isinstance(node, ast.Expression):
        _check(node.body)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise FormulaError(f"不支持的运算符: {type(node.op).__name__}")
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise FormulaError(f"不支持的运算符: {type(node.op).__name__}")
        _check(node.operand)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise FormulaError("公式中只允许数字常量")
    elif isinstance(node, ast.Name):
        if not node.id.isidentifier():
            raise FormulaError(f"非法变量名: {node.id}")
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            allowed = "/".join(_ALLOWED_FUNCS)
            raise FormulaError(f"仅允许调用函数: {allowed}")
        if node.keywords:
            raise FormulaError("不允许关键字参数")
        for arg in node.args:
            _check(arg)
    else:
        raise FormulaError(f"禁止的语法: {type(node).__name__}")


def validate_formula(formula: str, param_names: list) -> None:
    """校验公式语法与变量使用，不合法抛 FormulaError。

    公式中出现的变量必须都在 param_names（工具参数）或允许的函数名中。
    数据源取数字段在运行时才能确定，不在此处校验。
    """
    if not formula or not formula.strip():
        raise FormulaError("公式不能为空")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"公式语法错误: {e.msg}")
    _check(tree)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    unknown = used - set(param_names) - set(_ALLOWED_FUNCS)
    if unknown:
        raise FormulaError(f"公式中存在未定义的变量: {', '.join(sorted(unknown))}")


def evaluate(formula: str, values: dict):
    """代入变量求值，返回数值结果；失败抛 FormulaError。

    调用前应先用 validate_formula 校验。values 中的键作为变量注入，
    内置函数白名单同时可用；除此之外不暴露任何内置能力。
    """
    code = compile(formula.strip(), "<formula>", "eval")
    env = dict(_ALLOWED_FUNCS)
    env.update(values)
    try:
        result = eval(code, {"__builtins__": {}}, env)
    except ZeroDivisionError:
        raise FormulaError("公式计算出现除零")
    except FormulaError:
        raise
    except NameError as e:
        raise FormulaError(f"缺少变量: {e}")
    except OverflowError:
        raise FormulaError("计算结果溢出")
    except Exception as e:
        raise FormulaError(f"求值失败: {e}")
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise FormulaError("公式结果必须是数值")
    return result

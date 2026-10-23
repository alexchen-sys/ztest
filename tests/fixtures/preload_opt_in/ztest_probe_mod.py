"""Importable marker module: sits on sys.path but is never imported by the suite.

The preload opt-in fixture asserts on `sys.modules` whether the zygote imported
this module. The suite itself never imports it — any presence in sys.modules
comes from the preload path alone.
"""

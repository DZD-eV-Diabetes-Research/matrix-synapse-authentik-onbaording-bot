"""Async HTTP clients on a shared pooled base (AD-7): Authentik, Synapse-Admin, Matrix CS.

Base client + Authentik/Admin clients land in Phase 3; the Matrix CS client (+ Simplified Sliding
Sync stream and the concrete reconciler effectors) lands in Phase 4.
"""

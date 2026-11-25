"""Challenge management for Zync (replaces ctfd-management's challenges.sh).

Galvanize owns the challenge repositories: it clones and updates them, lists
the definitions, serves their files and builds the images on its Ansible
hosts. Zync reads definitions and files through Galvanize's admin API and
ingests, syncs and deletes the challenges in CTFd.
"""

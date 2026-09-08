# MLOps Engineering

A learning portfolio covering reproducible training, deployment, monitoring, and the ML lifecycle, developed through ITCS355 coursework (student u6688124). Instructor materials and starter code are attributed separately from student work.

## Work

- [Week 0 setup evidence](lab/lab0/README.md)
- [Lab 1 — starter and pending deliverables](lab/lab1/README.md)
- [Lab 2](lab/lab2/README.md)
- [Lab 3](lab/lab3/README.md)
- [Lab 4](lab/lab4/README.md)
- [Lab 5](lab/lab5/README.md)
- [Capstone](project/README.md)
- [Course materials and provenance](materials/README.md)
- [Reading notes](notes/technical-debt.md)

Run each lab from its own directory. For example:

```bash
cd lab/lab1
source .venv/bin/activate
make cloud-check
```

The root is one Git repository. Lab 1 is not complete: its training, adapter implementation, DVC setup, tracked experiments, dependency hashes, image digest and reproducibility evidence remain pending.

Repository: https://github.com/nithit-cypherX/mlops-engineering

For Lab 1, link directly to [lab/lab1](https://github.com/nithit-cypherX/mlops-engineering/tree/main/lab/lab1). Keep credentials, environments and local tracking stores out of Git. Existing Azure resources are shared with the local course setup.

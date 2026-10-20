import multiprocessing

# tqdm заводит multiprocessing-lock → resource_tracker перезапускает этот же exe;
# без freeze_support он поднял бы второй экземпляр приложения
multiprocessing.freeze_support()

from app.gui import main  # noqa: E402

main()

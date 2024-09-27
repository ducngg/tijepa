import time
import os
import json
import matplotlib.pyplot as plt
import torch

class Saver:
    
    SAVING_PATH = 'trains'
    
    def __init__(
        self, 
        metrics: list[str] = ['loss'],
        update_by: str = 'iter',
        folder_name: str = str(int(time.time())),
        **kwargs
    ):
        self.metrics = {
            metric: [] for metric in metrics
        }
        self.update_by = update_by
        self.folder_name = folder_name
        self.folder_path = os.path.join(Saver.SAVING_PATH, self.folder_name)
        
        self.configs = kwargs
        self.current_epoch = 1
        
        self.create_folder()
    
    def create_folder(self):
        if os.path.exists(self.folder_path):
            self.folder_path += '-' + str(int(time.time()))
        
        # Create train folder
        os.makedirs(self.folder_path)
        
        # Create json file
        self.json_configs_path = os.path.join(self.folder_path, 'configs.json')
        with open(self.json_configs_path, 'w') as f:
            json.dump(self.configs, f, indent=4)
        
        # Create train path
        for metric in self.metrics.keys():
            metric_imgs_path = os.path.join(self.folder_path, metric)
            os.makedirs(metric_imgs_path)
            
        
    def update_metric(self, items: dict):
        for key, value in items.items():
            if key not in self.metrics.keys():
                assert False, f"{key} is not a valid metric. Available metrics: {self.metrics.keys()}"
            self.metrics[key].append(value)
        
    def save_epoch(self):
        for metric, records in self.metrics.items():
            batches = list(range(1, len(records) + 1))

            # Plotting the loss function
            plt.figure(figsize=(8, 6))
            plt.plot(batches, records, label=f'{metric} per {self.update_by}', color='blue', marker='o', linestyle='-')
            plt.title(f'{metric} per {self.update_by}')
            plt.xlabel(self.update_by)
            plt.ylabel(metric)
            plt.grid(True)
            plt.legend()

            # Save the plot to the specified path
            plt.savefig(
                os.path.join(
                    self.folder_path, 
                    metric, 
                    f"epoch-{self.current_epoch}.png"
                ), 
                bbox_inches='tight'
            )  # Save the plot as loss.png in the assets folder

            # Clear the plot after saving
            plt.close()
            
        self.current_epoch += 1

    def save_checkpoint(self, save_dict: dict, epoch: int):
        torch.save(
            save_dict, 
            os.path.join(self.folder_path, f"epoch-{epoch}.pt")
        )
        

        
if not os.path.exists(Saver.SAVING_PATH):
    os.makedirs(Saver.SAVING_PATH)

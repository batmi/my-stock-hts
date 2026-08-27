import matplotlib.pyplot as plt
fig = plt.figure()
print("Figure facecolor:", fig.get_facecolor())
print("Savefig facecolor default:", plt.rcParams['savefig.facecolor'])
print("Savefig transparent default:", plt.rcParams['savefig.transparent'])
